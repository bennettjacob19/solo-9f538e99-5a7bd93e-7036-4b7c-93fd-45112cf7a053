"""End-to-end tests for the offline base-swap endpoint (POST /rebase)."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rebasing import APP_LABEL, NEW_BASE_LABEL, OLD_BASE_LABEL
from app.sample_image import (
    MT_CONFIG,
    MT_INDEX,
    MT_LAYER,
    MT_LAYER_GZ,
    MT_MANIFEST,
)

FIXED_MTIME = 1_700_000_000


# ---------------------------------------------------------------------------
# tiny OCI layout builder (everything explicit, no service internals)
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_tar(entries, gzip_layer=False):
    """entries: list of (name, kind, payload); kind in file/dir/symlink."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, kind, payload in entries:
            ti = tarfile.TarInfo(name)
            ti.mtime = FIXED_MTIME
            ti.uid = ti.gid = 0
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tf.addfile(ti)
            elif kind == "file":
                data = payload if isinstance(payload, bytes) else payload.encode()
                ti.type = tarfile.REGTYPE
                ti.mode = 0o644
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.mode = 0o777
                ti.linkname = payload
                tf.addfile(ti)
    raw = buf.getvalue()
    if gzip_layer:
        return gzip.compress(raw, compresslevel=9, mtime=FIXED_MTIME), MT_LAYER_GZ
    return raw, MT_LAYER


def build_layout(layers, diff_ids, history, platform=("linux", "amd64", ""),
                 config_extra=None, ref="img:latest"):
    """layers: list of (blob_bytes, media_type). Returns the layout tar bytes."""
    config = {
        "architecture": platform[1],
        "os": platform[0],
        "created": "2026-01-01T00:00:00Z",
        "config": {"Cmd": ["/bin/sh"], "Env": ["PATH=/usr/bin"]},
        "rootfs": {"type": "layers", "diff_ids": [f"sha256:{d}" for d in diff_ids]},
        "history": history,
    }
    if platform[2]:
        config["variant"] = platform[2]
    if config_extra:
        config.update(config_extra)
    config_b = json.dumps(config, sort_keys=True).encode()

    manifest = {
        "schemaVersion": 2,
        "mediaType": MT_MANIFEST,
        "config": {
            "mediaType": MT_CONFIG,
            "digest": f"sha256:{sha256_hex(config_b)}",
            "size": len(config_b),
        },
        "layers": [
            {"mediaType": mt,
             "digest": f"sha256:{sha256_hex(blob)}",
             "size": len(blob)}
            for blob, mt in layers
        ],
    }
    manifest_b = json.dumps(manifest, sort_keys=True).encode()
    index_b = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MT_INDEX,
            "manifests": [{
                "mediaType": MT_MANIFEST,
                "digest": f"sha256:{sha256_hex(manifest_b)}",
                "size": len(manifest_b),
                "annotations": {"org.opencontainers.image.ref.name": ref},
            }],
        },
        sort_keys=True,
    ).encode()

    blobs = [config_b, manifest_b] + [blob for blob, _ in layers]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in [
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            ("index.json", index_b),
        ]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for blob in blobs:
            ti = tarfile.TarInfo(f"blobs/sha256/{sha256_hex(blob)}")
            ti.size = len(blob)
            tf.addfile(ti, io.BytesIO(blob))
    return buf.getvalue()


def diff_id_of(blob, media_type):
    if media_type == MT_LAYER:
        return sha256_hex(blob)
    return sha256_hex(gzip.decompress(blob))


def hist(comment, empty=False, created="2026-01-01T00:00:00Z"):
    h = {"created": created, "comment": comment}
    if empty:
        h["empty_layer"] = True
    return h


def read_layout_files(data):
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf.getmembers():
            if m.isreg():
                files[m.name] = tf.extractfile(m).read()
    return files


def parse_layout(data):
    files = read_layout_files(data)
    index = json.loads(files["index.json"])
    mdesc = index["manifests"][0]
    manifest = json.loads(files[f"blobs/sha256/{mdesc['digest'].split(':')[1]}"])
    config = json.loads(
        files[f"blobs/sha256/{manifest['config']['digest'].split(':')[1]}"]
    )
    return files, index, manifest, config


# ---------------------------------------------------------------------------
# fixtures: old base (2 layers), new base (2 layers), app (old base + 1)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def images():
    # old base: layer A (gzip) + layer B (plain)
    a_blob, a_mt = make_tar([
        ("etc", "dir", None),
        ("etc/os-release", "file", b"old-base\n"),
        ("usr", "dir", None),
        ("usr/libold", "file", b"v1\n"),
    ], gzip_layer=True)
    b_blob, b_mt = make_tar([
        ("etc/patch-old", "file", b"old patch\n"),
    ])
    old_diff = [diff_id_of(a_blob, a_mt), diff_id_of(b_blob, b_mt)]
    old_hist = [hist("old base layer A"), hist("old base layer B")]
    old_tar = build_layout(
        [(a_blob, a_mt), (b_blob, b_mt)], old_diff, old_hist, ref="old:1"
    )

    # new base: same uncompressed layer A stored PLAIN (different compression
    # allowed), plus a patched layer B'
    a_plain_blob, a_plain_mt = make_tar([
        ("etc", "dir", None),
        ("etc/os-release", "file", b"old-base\n"),
        ("usr", "dir", None),
        ("usr/libold", "file", b"v1\n"),
    ])
    assert a_plain_mt == MT_LAYER and a_plain_blob != a_blob
    assert diff_id_of(a_plain_blob, a_plain_mt) == old_diff[0]
    bp_blob, bp_mt = make_tar([
        ("etc/patch-new", "file", b"new patch\n"),
        ("etc/os-release", "file", b"new-base\n"),
    ], gzip_layer=True)
    new_diff = [
        diff_id_of(a_plain_blob, a_plain_mt),
        diff_id_of(bp_blob, bp_mt),
    ]
    new_hist = [hist("new base layer A"), hist("new base patched B")]
    new_tar = build_layout(
        [(a_plain_blob, a_plain_mt), (bp_blob, bp_mt)],
        new_diff, new_hist, ref="new:2",
    )

    # app: same layer A (re-gzipped bytes differ, diff_id equal), layer B
    # identical, then app layer C
    a_gz2 = gzip.compress(gzip.decompress(a_blob), compresslevel=1, mtime=1)
    assert a_gz2 != a_blob
    c_blob, c_mt = make_tar([
        ("app", "dir", None),
        ("app/run.sh", "file", b"#!/bin/sh\necho hi\n"),
    ])
    app_diff = [
        diff_id_of(a_gz2, MT_LAYER_GZ),
        diff_id_of(b_blob, b_mt),
        diff_id_of(c_blob, c_mt),
    ]
    app_hist = [
        hist("old base layer A"),
        hist("old base layer B"),
        hist("application layer C"),
    ]
    app_tar = build_layout(
        [(a_gz2, MT_LAYER_GZ), (b_blob, b_mt), (c_blob, c_mt)],
        app_diff, app_hist, ref="app:9",
    )

    return {
        "app": app_tar,
        "old": old_tar,
        "new": new_tar,
        "blobs": {
            "app_c": c_blob,
            "old_a_gz": a_blob,
            "new_bp_gz": bp_blob,
            "new_a_plain": a_plain_blob,
        },
    }


def post_rebase(client, images, field="app_image"):
    return client.post(
        "/rebase",
        files={
            field: ("app.tar", images["app"], "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_rebase_happy_path(client, images):
    r = post_rebase(client, images)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/x-tar"
    data = r.content

    files, index, manifest, config = parse_layout(data)

    # single manifest, fresh descriptors
    assert len(index["manifests"]) == 1
    assert len(manifest["layers"]) == 3

    # diff_ids = new base's two + app layer C's diff_id
    app_files, _, app_manifest, app_config = parse_layout(images["app"])
    new_files, _, _, new_config = parse_layout(images["new"])
    assert config["rootfs"]["diff_ids"] == (
        new_config["rootfs"]["diff_ids"] + app_config["rootfs"]["diff_ids"][2:]
    )

    # history = new base history + app's own history tail
    assert config["history"] == (
        new_config["history"] + [hist("application layer C")]
    )

    # runtime configuration preserved (only rootfs/history changed)
    assert config["config"] == {"Cmd": ["/bin/sh"], "Env": ["PATH=/usr/bin"]}
    assert config["architecture"] == "amd64" and config["os"] == "linux"

    # app layer C blob bytes preserved verbatim
    c_desc = manifest["layers"][2]
    stored = files[f"blobs/sha256/{c_desc['digest'].split(':')[1]}"]
    assert stored == images["blobs"]["app_c"]
    assert c_desc["mediaType"] == MT_LAYER

    # every referenced blob exists with matching size and digest
    for desc in [manifest["config"], *manifest["layers"]]:
        p = f"blobs/sha256/{desc['digest'].split(':')[1]}"
        assert p in files
        assert len(files[p]) == desc["size"]
        assert sha256_hex(files[p]) == desc["digest"].split(":")[1]

    # self-contained: no blob without a reference, and oci-layout present
    assert files["oci-layout"] == b'{"imageLayoutVersion":"1.0.0"}'
    referenced = {
        f"blobs/sha256/{d['digest'].split(':')[1]}"
        for d in [manifest["config"], *manifest["layers"]]
    }
    referenced.add(
        f"blobs/sha256/{index['manifests'][0]['digest'].split(':')[1]}"
    )
    assert {n for n in files if n.startswith("blobs/")} == referenced


def test_rebased_archive_is_unpackable(client, images):
    r = post_rebase(client, images)
    assert r.status_code == 200, r.text
    up = client.post(
        "/unpack",
        files={"file": ("rebased.tar", r.content, "application/x-tar")},
    )
    assert up.status_code == 200, up.text
    fs = {}
    with tarfile.open(fileobj=io.BytesIO(up.content), mode="r:") as tf:
        for m in tf.getmembers():
            if m.isreg():
                fs[m.name] = tf.extractfile(m).read()
    # new base content wins, old base content is gone, app layer survives
    assert fs["etc/os-release"] == b"new-base\n"
    assert fs["etc/patch-new"] == b"new patch\n"
    assert "etc/patch-old" not in fs
    assert fs["app/run.sh"] == b"#!/bin/sh\necho hi\n"


def test_field_alias_app(client, images):
    r = post_rebase(client, images, field="app")
    assert r.status_code == 200, r.text


def test_missing_field_rejected(client, images):
    r = client.post(
        "/rebase",
        files={
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )
    assert r.status_code == 400
    assert "app_image" in r.json()["detail"]


# ---------------------------------------------------------------------------
# validation failures (errors only, no archive)
# ---------------------------------------------------------------------------


def test_diff_id_prefix_mismatch(client, images):
    # mutate the old base's first layer so its diff_id no longer prefixes app
    files = read_layout_files(images["old"])
    # rebuild old layout with a different first layer
    other_blob, other_mt = make_tar([("x", "file", b"different\n")])
    _, _, old_manifest, old_config = parse_layout(images["old"])
    b2_desc = old_manifest["layers"][1]
    b2_blob = files[f"blobs/sha256/{b2_desc['digest'].split(':')[1]}"]
    bad_old = build_layout(
        [(other_blob, other_mt), (b2_blob, b2_desc["mediaType"])],
        [diff_id_of(other_blob, other_mt), old_config["rootfs"]["diff_ids"][1].split(":")[1]],
        [hist("x"), hist("old base layer B")],
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", images["app"], "application/x-tar"),
            "old_base": ("old.tar", bad_old, "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert OLD_BASE_LABEL in detail and "diff_id" in detail and "layer 1" in detail


def test_history_prefix_mismatch(client, images):
    _, _, _, old_config = parse_layout(images["old"])
    _, _, old_manifest, _ = parse_layout(images["old"])
    files = read_layout_files(images["old"])
    blobs = [
        files[f"blobs/sha256/{d['digest'].split(':')[1]}"]
        for d in old_manifest["layers"]
    ]
    mts = [d["mediaType"] for d in old_manifest["layers"]]
    bad_hist = [hist("TAMPERED history"), hist("old base layer B")]
    bad_old = build_layout(
        list(zip(blobs, mts)),
        [d.split(":")[1] for d in old_config["rootfs"]["diff_ids"]],
        bad_hist,
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", images["app"], "application/x-tar"),
            "old_base": ("old.tar", bad_old, "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert OLD_BASE_LABEL in detail and "history" in detail


def test_platform_mismatch_rejected(client, images):
    _, _, _, new_config = parse_layout(images["new"])
    _, _, new_manifest, _ = parse_layout(images["new"])
    files = read_layout_files(images["new"])
    blobs = [
        files[f"blobs/sha256/{d['digest'].split(':')[1]}"]
        for d in new_manifest["layers"]
    ]
    mts = [d["mediaType"] for d in new_manifest["layers"]]
    arm_tar = build_layout(
        list(zip(blobs, mts)),
        [d.split(":")[1] for d in new_config["rootfs"]["diff_ids"]],
        copy.deepcopy(new_config["history"]),
        platform=("linux", "arm64", ""),
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", images["app"], "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", arm_tar, "application/x-tar"),
        },
    )
    assert r.status_code == 400
    assert "platform" in r.json()["detail"]


def test_missing_history_rejected(client, images):
    # config without history -> every input must carry one
    _, _, _, app_config = parse_layout(images["app"])
    _, _, app_manifest, _ = parse_layout(images["app"])
    files = read_layout_files(images["app"])
    blobs = [
        files[f"blobs/sha256/{d['digest'].split(':')[1]}"]
        for d in app_manifest["layers"]
    ]
    mts = [d["mediaType"] for d in app_manifest["layers"]]
    bad = build_layout(
        list(zip(blobs, mts)),
        [d.split(":")[1] for d in app_config["rootfs"]["diff_ids"]],
        [],
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", bad, "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )
    assert r.status_code == 400
    assert "history" in r.json()["detail"]


def test_empty_layer_history_ok(client, images):
    # empty_layer entries occupy history slots but no layer; still a prefix
    _, _, _, app_config = parse_layout(images["app"])
    _, _, app_manifest, _ = parse_layout(images["app"])
    files = read_layout_files(images["app"])
    blobs = [
        files[f"blobs/sha256/{d['digest'].split(':')[1]}"]
        for d in app_manifest["layers"]
    ]
    mts = [d["mediaType"] for d in app_manifest["layers"]]
    hist_with_empty = [
        hist("old base layer A"),
        hist("no-op", empty=True),
        hist("old base layer B"),
        hist("application layer C"),
    ]
    app2 = build_layout(
        list(zip(blobs, mts)),
        [d.split(":")[1] for d in app_config["rootfs"]["diff_ids"]],
        hist_with_empty,
    )
    old2 = build_layout(
        list(zip(blobs[:2], mts[:2])),
        [d.split(":")[1] for d in app_config["rootfs"]["diff_ids"][:2]],
        [hist("old base layer A"), hist("no-op", empty=True),
         hist("old base layer B")],
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", app2, "application/x-tar"),
            "old_base": ("old.tar", old2, "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )
    assert r.status_code == 200, r.text
    _, _, _, cfg = parse_layout(r.content)
    # new base history (2 entries) + app tail past the 3-entry old prefix
    assert cfg["history"] == (
        [hist("new base layer A"), hist("new base patched B")]
        + [hist("application layer C")]
    )
    assert len(cfg["rootfs"]["diff_ids"]) == 3


def test_corrupt_blob_rejected(client, images):
    files = read_layout_files(images["new"])
    # flip a byte in one layer blob without touching the descriptor
    _, _, new_manifest, _ = parse_layout(images["new"])
    target = f"blobs/sha256/{new_manifest['layers'][0]['digest'].split(':')[1]}"
    blob = bytearray(files[target])
    blob[0] ^= 0x01
    files[target] = bytes(blob)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", images["app"], "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", buf.getvalue(), "application/x-tar"),
        },
    )
    assert r.status_code == 400
    assert NEW_BASE_LABEL in r.json()["detail"]


def test_app_without_own_layer_rejected(client, images):
    # app identical to old base: nothing to rebase onto
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", images["old"], "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )
    assert r.status_code == 400
    assert "at least one layer" in r.json()["detail"]


# ---------------------------------------------------------------------------
# overlay failure -> structured error mapped to source package
# ---------------------------------------------------------------------------


def test_overlay_error_reports_app_layer_and_path(client, images):
    # app layer C contains an entry that escapes via '..'; trial unpack must
    # fail and map it back to app_image, original layer 3, entry path shown
    bad_c, bad_c_mt = make_tar([("../escape.txt", "file", b"pwned\n")])
    a_blob = images["blobs"]["old_a_gz"]
    _, _, old_manifest, old_config = parse_layout(images["old"])
    old_files = read_layout_files(images["old"])
    b_blob = old_files[
        f"blobs/sha256/{old_manifest['layers'][1]['digest'].split(':')[1]}"
    ]
    app_bad = build_layout(
        [(a_blob, MT_LAYER_GZ), (b_blob, MT_LAYER), (bad_c, bad_c_mt)],
        [
            old_config["rootfs"]["diff_ids"][0].split(":")[1],
            old_config["rootfs"]["diff_ids"][1].split(":")[1],
            diff_id_of(bad_c, bad_c_mt),
        ],
        [hist("old base layer A"), hist("old base layer B"),
         hist("bad app layer")],
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", app_bad, "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["error"] == "layer_overlay_failed"
    assert detail["source_package"] == APP_LABEL
    assert detail["original_layer_number"] == 3
    assert "../escape.txt" in detail["entry_path"]


def test_different_new_base_layer_count_keeps_app_numbering(client, images):
    # New base has THREE layers while the old base had two. An overlay
    # failure in the app's own layer must still report its ORIGINAL app
    # manifest layer number (3), not the rebased position (4).
    a_blob = images["blobs"]["new_a_plain"]
    extra_blob, extra_mt = make_tar([("etc/extra", "file", b"e\n")])
    bp_blob, bp_mt = make_tar([
        ("etc/patch-new", "file", b"new patch\n"),
        ("etc/os-release", "file", b"new-base\n"),
    ], gzip_layer=True)
    three_layer_new = build_layout(
        [(a_blob, MT_LAYER), (extra_blob, extra_mt), (bp_blob, bp_mt)],
        [
            diff_id_of(a_blob, MT_LAYER),
            diff_id_of(extra_blob, extra_mt),
            diff_id_of(bp_blob, bp_mt),
        ],
        [hist("n1"), hist("n2"), hist("n3")],
    )

    bad_c, bad_c_mt = make_tar([("../escape.txt", "file", b"pwned\n")])
    _, _, old_manifest, old_config = parse_layout(images["old"])
    old_files = read_layout_files(images["old"])
    old_a = images["blobs"]["old_a_gz"]
    old_b = old_files[
        f"blobs/sha256/{old_manifest['layers'][1]['digest'].split(':')[1]}"
    ]
    app_bad = build_layout(
        [(old_a, MT_LAYER_GZ), (old_b, MT_LAYER), (bad_c, bad_c_mt)],
        [
            old_config["rootfs"]["diff_ids"][0].split(":")[1],
            old_config["rootfs"]["diff_ids"][1].split(":")[1],
            diff_id_of(bad_c, bad_c_mt),
        ],
        [hist("old base layer A"), hist("old base layer B"), hist("bad app")],
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", app_bad, "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", three_layer_new, "application/x-tar"),
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["source_package"] == APP_LABEL
    assert detail["original_layer_number"] == 3


def test_same_layer_different_compression_accepted(client, images):
    # the fixture already stores layer A as gzip in the app but plain in the
    # new base; the happy path output must keep both descriptors/media types
    r = post_rebase(client, images)
    assert r.status_code == 200, r.text
    _, _, manifest, _ = parse_layout(r.content)
    media_types = [d["mediaType"] for d in manifest["layers"]]
    assert media_types == [MT_LAYER, MT_LAYER_GZ, MT_LAYER]


def test_overlay_error_reports_new_base_layer(client, images):
    # corrupt the new base's second layer tar (valid gzip, invalid tar):
    # blob/digest descriptors are regenerated consistently, diff_id matches,
    # but trial-unpack applying layer 2 fails -> maps to new_base layer 2
    a_blob = images["blobs"]["new_a_plain"]
    bad_tar_gz = gzip.compress(b"not a tar at all", mtime=0)
    new_bad = build_layout(
        [(a_blob, MT_LAYER), (bad_tar_gz, MT_LAYER_GZ)],
        [diff_id_of(a_blob, MT_LAYER), diff_id_of(bad_tar_gz, MT_LAYER_GZ)],
        [hist("new base layer A"), hist("broken layer")],
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", images["app"], "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", new_bad, "application/x-tar"),
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["source_package"] == NEW_BASE_LABEL
    assert detail["original_layer_number"] == 2


def test_unpack_error_carries_layer_number_and_path(client, images):
    # direct /unpack on a crafted image: error mentions 1-based layer & path
    bad_layer, mt = make_tar([("ok", "file", b"x"), ("../x", "file", b"y")])
    good, good_mt = make_tar([("base", "file", b"b\n")])
    image = build_layout(
        [(good, good_mt), (bad_layer, mt)],
        [diff_id_of(good, good_mt), diff_id_of(bad_layer, mt)],
        [hist("l1"), hist("l2")],
    )
    r = client.post(
        "/unpack",
        files={"file": ("img.tar", image, "application/x-tar")},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "layer 2" in detail
    assert "../x" in detail


# ---------------------------------------------------------------------------
# streaming / memory behaviour
# ---------------------------------------------------------------------------


def test_large_layer_streamed_without_buffering(client, images, tmp_path):
    # A ~30 MiB layer round-trips through rebase with its bytes preserved;
    # the service itself copies in CHUNK blocks and streams via FileResponse.
    import app.rebasing as reb

    big = tmp_path / "biglayer.tar"
    block = b"x" * (1024 * 1024)  # 1 MiB block, repeated 30 times = 30 MiB
    total = len(block) * 30
    with tarfile.open(big, "w") as tf:
        ti = tarfile.TarInfo("big.bin")
        ti.size = total

        class _Repeat:
            def __init__(self, data, times):
                self.data = data
                self.remaining = times
                self.offset = 0

            def read(self, n=-1):
                if self.remaining == 0:
                    return b""
                if n is None or n < 0:
                    n = len(self.data) - self.offset
                chunk = self.data[self.offset:self.offset + n]
                self.offset += len(chunk)
                if self.offset >= len(self.data):
                    self.remaining -= 1
                    self.offset = 0
                return chunk

        tf.addfile(ti, _Repeat(block, 30))
    layer_blob = big.read_bytes()
    assert len(layer_blob) > total

    app_files, _, app_manifest, app_config = parse_layout(images["app"])
    base_blobs = [
        app_files[f"blobs/sha256/{d['digest'].split(':')[1]}"]
        for d in app_manifest["layers"][:2]
    ]
    base_mts = [d["mediaType"] for d in app_manifest["layers"][:2]]
    big_app = build_layout(
        [*zip(base_blobs, base_mts), (layer_blob, MT_LAYER)],
        [
            *[d.split(":")[1] for d in app_config["rootfs"]["diff_ids"][:2]],
            sha256_hex(layer_blob),
        ],
        [hist("old base layer A"), hist("old base layer B"), hist("big")],
    )
    r = client.post(
        "/rebase",
        files={
            "app_image": ("app.tar", big_app, "application/x-tar"),
            "old_base": ("old.tar", images["old"], "application/x-tar"),
            "new_base": ("new.tar", images["new"], "application/x-tar"),
        },
    )
    assert r.status_code == 200, r.text
    _, _, manifest, _ = parse_layout(r.content)
    assert manifest["layers"][2]["size"] == len(layer_blob)
    # chunked, bounded streaming primitives are what keep memory flat
    assert reb.CHUNK <= 1024 * 1024
