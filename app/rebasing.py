"""Rebase an application image onto a new base image (offline base swap).

Three same-platform, single-manifest OCI Image Layout tars are combined:

* the **application image** -- old base layers plus the app's own layers;
* the **old base** -- the bottom of the application image;
* the **new base** -- the patched replacement for the old base.

The old base's ``rootfs.diff_ids`` and non-empty ``history`` entries must
each be a strict prefix of the application image's (the same uncompressed
layer may be stored with a different compression, hence comparison happens
on diff_ids, never on blob digests).  All three packages must carry
history; ``empty_layer`` history entries only occupy history slots while
every other history entry pairs 1:1 with a layer.

The output is again a self-contained single-manifest OCI Image Layout tar:
new-base layers first, then the application's own layers with their
*original blob bytes* (byte-for-byte copy, original media type) and the
matching history entries.  The application's runtime configuration is
preserved -- only ``rootfs.diff_ids`` and ``history`` are rewritten -- and
every descriptor (config, layers, manifest, index) is regenerated.

Before the archive is offered for download it is trial-unpacked through
:func:`app.unpacker.unpack_layout_dir`; when layers cannot be overlaid the
error is mapped back to the originating input package with the original
1-based layer number and the offending entry path.

Everything streams: uploads land on disk, blobs are copied in
:data:`~app.unpacker.CHUNK` blocks (hardlinks when the filesystem allows),
and the output tar is written member by member.  No whole package or
whole layer is ever buffered in memory.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shutil
import tarfile

from .unpacker import (
    CHUNK,
    LayerApplicationError,
    UnpackError,
    extract_layout,
    inspect_layout,
    unpack_layout_dir,
)

APP_LABEL = "app_image"
OLD_BASE_LABEL = "old_base"
NEW_BASE_LABEL = "new_base"

OCI_LAYOUT_BYTES = b'{"imageLayoutVersion":"1.0.0"}'
MT_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_INDEX = "application/vnd.oci.image.index.v1+json"


class RebaseError(Exception):
    """Any validation failure while rebasing; the request must fail."""


class RebaseOverlayError(Exception):
    """Trial unpack of the rebased image failed while applying a layer.

    Carries the originating input package (``app_image`` / ``new_base``),
    the 1-based layer number *within that package* and the entry path.
    """

    def __init__(self, source_package, original_layer_number, entry_path, message):
        self.source_package = source_package
        self.original_layer_number = original_layer_number
        self.entry_path = entry_path
        self.message = message
        super().__init__(
            f"{source_package} layer {original_layer_number} "
            f"entry {entry_path!r}: {message}"
        )


# ---------------------------------------------------------------------------
# input handling / validation
# ---------------------------------------------------------------------------


def _extract_and_inspect(tar_path, workdir, name, label):
    layout_dir = os.path.join(workdir, name)
    os.makedirs(layout_dir)
    try:
        extract_layout(tar_path, layout_dir)
        return inspect_layout(layout_dir, label=label)
    except UnpackError as e:
        raise RebaseError(str(e))


def _platform_of(bundle):
    cfg = bundle.config
    return cfg.get("os"), cfg.get("architecture"), cfg.get("variant") or ""


def _check_prefixes(app, old):
    """Old base diff_ids and history must be prefixes of the app image's."""
    n_old = len(old.diff_ids)
    n_app = len(app.diff_ids)
    if n_old == 0:
        raise RebaseError(f"{OLD_BASE_LABEL}: base image has no layers")
    if n_old >= n_app:
        raise RebaseError(
            f"{APP_LABEL}: application image must add at least one layer on "
            f"top of {OLD_BASE_LABEL} (app layers={n_app}, base layers={n_old})"
        )

    for i in range(n_old):
        if old.diff_ids[i] != app.diff_ids[i]:
            raise RebaseError(
                f"{OLD_BASE_LABEL}: diff_id at layer {i + 1} "
                f"(sha256:{old.diff_ids[i]}) is not a prefix of "
                f"{APP_LABEL} (sha256:{app.diff_ids[i]})"
            )

    old_nonempty = [h for h in old.config["history"] if h.get("empty_layer") is not True]
    app_nonempty = [h for h in app.config["history"] if h.get("empty_layer") is not True]
    for i, entry in enumerate(old_nonempty):
        if entry != app_nonempty[i]:
            raise RebaseError(
                f"{OLD_BASE_LABEL}: history entry {i + 1} is not a prefix of "
                f"{APP_LABEL} history"
            )


def _validate_trio(app, old, new):
    platforms = {
        APP_LABEL: _platform_of(app),
        OLD_BASE_LABEL: _platform_of(old),
        NEW_BASE_LABEL: _platform_of(new),
    }
    if len(set(platforms.values())) != 1:
        raise RebaseError(
            "all three images must share the same platform (os/architecture"
            "/variant): " + ", ".join(f"{k}={v}" for k, v in platforms.items())
        )
    _check_prefixes(app, old)


# ---------------------------------------------------------------------------
# output assembly
# ---------------------------------------------------------------------------


def _write_json_blob(out_blobs, payload):
    """Stage a JSON blob under out_blobs; return (digest, size, path)."""
    hexd = hashlib.sha256(payload).hexdigest()
    path = os.path.join(out_blobs, hexd)
    if not os.path.exists(path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(payload)
        os.replace(tmp, path)
    return f"sha256:{hexd}", len(payload), path


def _stage_layer_blob(src_path, digest, out_blobs):
    """Place a layer blob into the output layout without loading it.

    Hardlink first (same filesystem, zero copy), fall back to chunked
    streaming copy.  Source blobs were already size/digest verified.
    """
    hexd = digest.split(":", 1)[1]
    dst = os.path.join(out_blobs, hexd)
    if os.path.exists(dst):
        return dst
    try:
        os.link(src_path, dst)
        return dst
    except OSError:
        pass
    tmp = dst + ".tmp"
    with open(src_path, "rb") as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out, CHUNK)
    os.replace(tmp, dst)
    return dst


def _add_regular(tf, name, fileobj, size):
    ti = tarfile.TarInfo(name)
    ti.size = size
    ti.mode = 0o644
    tf.addfile(ti, fileobj)


def _add_dir(tf, name):
    ti = tarfile.TarInfo(name if name.endswith("/") else name + "/")
    ti.type = tarfile.DIRTYPE
    ti.mode = 0o755
    tf.addfile(ti)


def _stream_file_into_tar(tf, arcname, path):
    """Add a regular file to tar *tf* streaming in CHUNK-sized reads."""
    size = os.path.getsize(path)
    ti = tarfile.TarInfo(arcname)
    ti.size = size
    ti.mode = 0o644
    with open(path, "rb") as f:
        tf.addfile(ti, f)


def build_rebased_archive(app, old_base, new_base, workdir, out_tar_path):
    """Assemble the rebased OCI Image Layout tar at *out_tar_path*.

    Returns ``(output_layout_dir, n_new, n_old)`` so the caller can run the
    trial unpack and map rebased layer numbers back to the input packages.
    """
    n_new = len(new_base.diff_ids)
    n_old = len(old_base.diff_ids)

    # --- config: keep the application runtime config, swap rootfs/history
    new_config = copy.deepcopy(app.config)
    new_config["rootfs"] = copy.deepcopy(app.config.get("rootfs") or {})
    new_config["rootfs"]["type"] = new_config["rootfs"].get("type", "layers")
    # new-base diff_ids followed by the app's own layer diff_ids (those past
    # the old-base prefix)
    new_config["rootfs"]["diff_ids"] = [
        f"sha256:{d}" for d in (new_base.diff_ids + app.diff_ids[n_old:])
    ]
    # history: the new base's full history (including its empty_layer
    # slots), then the application's own entries past the old-base prefix.
    # The old base's non-empty history is a proven prefix of the app's, so
    # the total non-empty count still matches the layer count 1:1.
    new_config["history"] = (
        copy.deepcopy(new_base.config["history"])
        + copy.deepcopy(app.config["history"][len(old_base.config["history"]):])
    )
    config_b = json.dumps(new_config, sort_keys=True).encode("utf-8")

    # --- stage blobs into a fresh OCI layout directory
    out_layout = os.path.join(workdir, "rebased-layout")
    out_blobs = os.path.join(out_layout, "blobs", "sha256")
    os.makedirs(out_blobs, exist_ok=True)

    config_digest, config_size, _ = _write_json_blob(out_blobs, config_b)

    out_layers = []
    # new base layers: descriptors taken verbatim from the new base manifest
    for ldesc in new_base.layer_descriptors:
        _stage_layer_blob(
            new_base.blob_path(ldesc["digest"]), ldesc["digest"], out_blobs
        )
        out_layers.append(copy.deepcopy(ldesc))
    # application's own layers: original blob bytes, original descriptor
    for ldesc in app.layer_descriptors[n_old:]:
        _stage_layer_blob(app.blob_path(ldesc["digest"]), ldesc["digest"], out_blobs)
        out_layers.append(copy.deepcopy(ldesc))

    new_manifest = {
        "schemaVersion": 2,
        "mediaType": app.manifest.get("mediaType", MT_MANIFEST),
        "config": {
            "mediaType": app.config_descriptor.get(
                "mediaType", "application/vnd.oci.image.config.v1+json"
            ),
            "digest": config_digest,
            "size": config_size,
        },
        "layers": out_layers,
    }
    if "annotations" in app.manifest:
        new_manifest["annotations"] = copy.deepcopy(app.manifest["annotations"])
    manifest_b = json.dumps(new_manifest, sort_keys=True).encode("utf-8")
    manifest_digest, manifest_size, _ = _write_json_blob(out_blobs, manifest_b)

    index_descriptor = {
        "mediaType": app.manifest_descriptor.get("mediaType", MT_MANIFEST),
        "digest": manifest_digest,
        "size": manifest_size,
    }
    if "annotations" in app.manifest_descriptor:
        index_descriptor["annotations"] = copy.deepcopy(
            app.manifest_descriptor["annotations"]
        )
    index_b = json.dumps(
        {"schemaVersion": 2, "mediaType": MT_INDEX, "manifests": [index_descriptor]},
        sort_keys=True,
    ).encode("utf-8")

    # metadata files also live in the on-disk layout so the trial unpack can
    # verify the exact same content that will be offered for download
    with open(os.path.join(out_layout, "oci-layout"), "wb") as f:
        f.write(OCI_LAYOUT_BYTES)
    with open(os.path.join(out_layout, "index.json"), "wb") as f:
        f.write(index_b)

    # --- stream the self-contained layout tar (blobs never enter memory)
    tmp_tar = out_tar_path + ".tmp"
    with tarfile.open(tmp_tar, "w", format=tarfile.PAX_FORMAT) as tf:
        _add_regular(tf, "oci-layout", io.BytesIO(OCI_LAYOUT_BYTES),
                     len(OCI_LAYOUT_BYTES))
        _add_regular(tf, "index.json", io.BytesIO(index_b), len(index_b))
        _add_dir(tf, "blobs")
        _add_dir(tf, "blobs/sha256")
        for name in sorted(os.listdir(out_blobs)):
            _stream_file_into_tar(
                tf, f"blobs/sha256/{name}", os.path.join(out_blobs, name)
            )
    os.replace(tmp_tar, out_tar_path)
    return out_layout, n_new, n_old


# ---------------------------------------------------------------------------
# trial unpack
# ---------------------------------------------------------------------------


def _trial_unpack(out_layout, n_new, n_old, workdir):
    """Trial-unpack the rebased layout; map failures back to the inputs.

    Rebased manifest order: ``[new-base layers 1..n_new]`` then the app's
    own layers.  An app-origin layer at rebased position *k* was layer
    ``n_old + (k - n_new)`` in the application image's original manifest.
    Both entry-level failures (:class:`LayerApplicationError`) and an
    unreadable/corrupt layer tar are mapped -- neither kind may leave the
    service without a source-package/layer-number attribution.
    """
    trial_dir = os.path.join(workdir, "trial")
    os.makedirs(trial_dir)
    try:
        unpack_layout_dir(out_layout, trial_dir)
    except LayerApplicationError as e:
        if e.layer_number <= n_new:
            source = NEW_BASE_LABEL
            original_no = e.layer_number
        else:
            source = APP_LABEL
            original_no = n_old + (e.layer_number - n_new)
        raise RebaseOverlayError(
            source, original_no, e.entry_path, e.message
        )
    except UnpackError as e:
        mapped = _map_layer_corruption(str(e), n_new, n_old)
        if mapped is not None:
            # No entry could be read: the whole layer tar is unreadable, so
            # there is no entry path to attribute the failure to.
            raise RebaseOverlayError(mapped[0], mapped[1], None, str(e))
        raise RebaseError(f"trial unpack of rebased image failed: {e}")
    finally:
        shutil.rmtree(trial_dir, ignore_errors=True)


def _map_layer_corruption(message, n_new, n_old):
    """Extract a 1-based layer number from a 'layer N: corrupt layer tar'
    error and map it to (source package, original layer number), or None."""
    match = re.match(r"layer (\d+): corrupt layer tar", message)
    if not match:
        return None
    layer_no = int(match.group(1))
    if layer_no <= n_new:
        return NEW_BASE_LABEL, layer_no
    return APP_LABEL, n_old + (layer_no - n_new)


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------


def rebase_image(app_tar, old_base_tar, new_base_tar, workdir, out_tar_path):
    """Validate the three inputs, build the rebased tar and trial-unpack it.

    Raises :class:`RebaseError` for validation failures and
    :class:`RebaseOverlayError` for layer-overlay failures (with input
    package / original layer number / entry path).
    """
    app = _extract_and_inspect(app_tar, workdir, "app", APP_LABEL)
    old = _extract_and_inspect(old_base_tar, workdir, "old-base", OLD_BASE_LABEL)
    new = _extract_and_inspect(new_base_tar, workdir, "new-base", NEW_BASE_LABEL)

    _validate_trio(app, old, new)

    out_layout, n_new, n_old = build_rebased_archive(
        app, old, new, workdir, out_tar_path
    )
    _trial_unpack(out_layout, n_new, n_old, workdir)
    return out_tar_path
