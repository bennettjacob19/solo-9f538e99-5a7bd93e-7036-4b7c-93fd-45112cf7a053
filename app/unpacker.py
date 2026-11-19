"""Reconstruct an OCI image rootfs from a single-manifest OCI Image Layout tar.

Guarantees provided by this module:

* every blob referenced by index.json / the manifest is checked for
  presence, exact size and SHA-256;
* every layer's ``diff_id`` from the image config is verified against the
  (decompressed) layer tar bytes;
* layers are applied in manifest order, restoring regular files,
  directories, symlinks, hardlinks and permission bits;
* whiteouts (``.wh.<name>``) and opaque markers (``.wh..wh..opq``) only
  remove *lower*-layer content.  A marker never deletes content that is
  added by the same layer, no matter where the marker sits inside the
  layer tar (deletions are computed for the whole layer first, then
  applied to the lower filesystem state, then new entries are written);
* overwriting one end of a hardlink pair never changes the other end,
  because files are replaced by rename, never truncated in place;
* no entry path, symlink target or hardlink target can cause a write
  outside the per-request rootfs directory;
* any missing blob, corruption or illegal path raises :class:`UnpackError`
  and the whole request fails -- no partial filesystem is produced.

Layer-application failures raise :class:`LayerApplicationError`, which
carries the 1-based manifest layer number and the offending entry path so
callers (e.g. the rebase endpoint) can map the failure back to the
original input package.  Nothing ever buffers a whole archive or a whole
layer in memory: uploads, blob hashing, gzip decompression and tar
production all stream in :data:`CHUNK`-sized blocks.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass, field

CHUNK = 1024 * 1024
MAX_SYMLINK_DEPTH = 255

TAR_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.docker.image.rootfs.diff.tar",
}
GZIP_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}

WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"


class UnpackError(Exception):
    """Any invalid, missing or corrupt input. Aborts the whole request."""


class LayerApplicationError(UnpackError):
    """An entry of one layer could not be applied.

    ``layer_number`` is 1-based (the position in the image manifest) and
    ``entry_path`` is the offending tar member's normalized path.
    """

    def __init__(self, layer_number, entry_path, message):
        self.layer_number = layer_number
        self.entry_path = entry_path
        full = f"layer {layer_number} entry {entry_path!r}: {message}"
        super().__init__(full)
        # keep the underlying message handy for structured responses
        self.message = message


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------


def _parse_digest(value, _what=""):
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise UnpackError(f"unsupported or missing digest: {value!r}")
    hexd = value[len("sha256:"):]
    if len(hexd) != 64 or any(c not in "0123456789abcdef" for c in hexd):
        raise UnpackError(f"malformed sha256 digest: {value!r}")
    return hexd


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


def _read_json(path, what):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        raise UnpackError(f"missing {what}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise UnpackError(f"invalid {what}: {e}")


# ---------------------------------------------------------------------------
# outer tar (the uploaded OCI Image Layout archive)
# ---------------------------------------------------------------------------


def _normalize_archive_name(name, what):
    """Lexically normalize a tar member name; reject anything unsafe."""
    if not isinstance(name, str) or "\x00" in name:
        raise UnpackError(f"{what}: invalid name {name!r}")
    if name.startswith("/"):
        raise UnpackError(f"{what}: absolute path not allowed: {name!r}")
    parts = []
    for p in name.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise UnpackError(f"{what}: '..' not allowed: {name!r}")
        parts.append(p)
    return parts


def extract_layout(upload_path, dest):
    """Extract the uploaded layout tar into *dest* (regular files/dirs only)."""
    try:
        tf = tarfile.open(upload_path, "r:*")
    except (tarfile.TarError, OSError) as e:
        raise UnpackError(f"cannot read uploaded tar: {e}")
    with tf:
        for member in tf:
            parts = _normalize_archive_name(member.name, "image layout tar")
            if not parts:
                continue
            target = os.path.join(dest, *parts)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isreg():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    raise UnpackError(f"cannot read {member.name!r} from upload")
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out, CHUNK)
            else:
                raise UnpackError(
                    f"image layout tar: unsupported entry type for {member.name!r}"
                )


# ---------------------------------------------------------------------------
# descriptor / blob / diff_id verification
# ---------------------------------------------------------------------------


def _verify_blob(path, descriptor, what):
    """Check presence, size and sha256 of one blob. Returns its hex digest."""
    if not isinstance(descriptor, dict):
        raise UnpackError(f"{what}: invalid descriptor")
    expected = _parse_digest(descriptor.get("digest"), what)
    if not os.path.isfile(path):
        raise UnpackError(f"{what}: missing blob sha256:{expected}")
    size = descriptor.get("size")
    if not isinstance(size, int) or isinstance(size, bool):
        raise UnpackError(f"{what}: descriptor missing size")
    actual_size = os.path.getsize(path)
    if actual_size != size:
        raise UnpackError(
            f"{what}: size mismatch for sha256:{expected} "
            f"(expected {size}, got {actual_size})"
        )
    actual = _sha256_file(path)
    if actual != expected:
        raise UnpackError(f"{what}: sha256 mismatch for blob sha256:{expected}")
    return actual


def _stream_gunzip(blob_path, sink=None):
    """Decompress a gzip blob in chunks; SHA-256 the uncompressed bytes.

    When *sink* is an open binary file, the uncompressed stream is copied
    into it as well.  Only :data:`CHUNK`-sized blocks are held in memory.
    Returns the hex digest of the uncompressed content.
    """
    h = hashlib.sha256()
    try:
        with gzip.open(blob_path, "rb") as src:
            for blk in iter(lambda: src.read(CHUNK), b""):
                h.update(blk)
                if sink is not None:
                    sink.write(blk)
    except (OSError, EOFError) as e:
        raise UnpackError(f"gzip decompression failed: {e}")
    return h.hexdigest()


def _diff_id_of_blob(blob_path, media_type):
    """Compute the uncompressed SHA-256 (diff_id) of one layer blob."""
    if media_type in TAR_MEDIA_TYPES:
        return _sha256_file(blob_path)
    if media_type in GZIP_MEDIA_TYPES:
        return _stream_gunzip(blob_path)
    raise UnpackError(f"unsupported mediaType {media_type!r}")


@dataclass
class LayoutBundle:
    """Everything an inspected single-manifest OCI Image Layout contains."""

    layout_dir: str
    index: dict
    manifest_descriptor: dict
    manifest: dict
    config_descriptor: dict
    config: dict
    diff_ids: list = field(default_factory=list)  # hex strings, manifest order
    layer_descriptors: list = field(default_factory=list)
    # plain (decompressed) tar paths, same order; None entries mean "use the
    # blob directly" (uncompressed layer or no decompress_dir requested)
    prepared_tar_paths: list = field(default_factory=list)

    def blob_path(self, digest):
        hexd = digest.split(":", 1)[1] if digest.startswith("sha256:") else digest
        return os.path.join(self.layout_dir, "blobs", "sha256", hexd)


def inspect_layout(layout_dir, label="", decompress_dir=None):
    """Parse and fully verify a layout on disk; return a :class:`LayoutBundle`.

    The check covers: single manifest in index.json, presence/size/SHA-256
    of every referenced blob, config ``rootfs.diff_ids`` against the
    (decompressed) layer bytes, the history requirement (present, and the
    non-empty entries pairing 1:1 with layers) and the platform fields.
    Every error is prefixed with *label* (e.g. the input package name).

    When *decompress_dir* is given, gzip layers are streamed into plain
    tar files there in the same pass used for diff_id verification, so a
    later unpack never has to gunzip a layer a second time.
    """
    prefix = f"{label}: " if label else ""

    def fail(msg):
        raise UnpackError(prefix + msg)

    if not os.path.isfile(os.path.join(layout_dir, "oci-layout")):
        fail("missing oci-layout")
    layout_marker = _read_json(
        os.path.join(layout_dir, "oci-layout"), "oci-layout"
    )
    if not isinstance(layout_marker, dict):
        fail("oci-layout is not a JSON object")
    index = _read_json(os.path.join(layout_dir, "index.json"), "index.json")
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(manifests, list) or not manifests:
        fail("index.json: no manifests")
    if len(manifests) != 1:
        fail(f"only single-manifest layouts are supported (got {len(manifests)})")

    mdesc = manifests[0]
    if not isinstance(mdesc, dict):
        fail("index.json: invalid manifest descriptor")
    mhex = _parse_digest(mdesc.get("digest"), prefix + "manifest")
    mpath = os.path.join(layout_dir, "blobs", "sha256", mhex)
    try:
        _verify_blob(mpath, mdesc, prefix + "manifest")
    except UnpackError as e:
        fail(str(e))
    manifest = _read_json(mpath, "manifest")
    if not isinstance(manifest, dict):
        fail("manifest is not a JSON object")

    cdesc = manifest.get("config")
    if not isinstance(cdesc, dict):
        fail("manifest: missing config descriptor")
    chex = _parse_digest(cdesc.get("digest"), prefix + "config")
    cpath = os.path.join(layout_dir, "blobs", "sha256", chex)
    try:
        _verify_blob(cpath, cdesc, prefix + "config")
    except UnpackError as e:
        fail(str(e))
    config = _read_json(cpath, "config")
    if not isinstance(config, dict):
        fail("config is not a JSON object")

    rootfs_cfg = config.get("rootfs")
    diff_ids_raw = rootfs_cfg.get("diff_ids") if isinstance(rootfs_cfg, dict) else None
    if not isinstance(diff_ids_raw, list):
        fail("config: missing rootfs.diff_ids")
    diff_ids = [_parse_digest(d, prefix + "config diff_id") for d in diff_ids_raw]

    layers = manifest.get("layers")
    if not isinstance(layers, list):
        fail("manifest: missing layers")
    if len(layers) != len(diff_ids):
        fail(
            f"manifest layers ({len(layers)}) and config rootfs.diff_ids "
            f"({len(diff_ids)}) count mismatch"
        )

    # All three packages must carry history; empty_layer entries only take
    # up a history slot, every other history entry pairs with one layer.
    history = config.get("history")
    if not isinstance(history, list) or not history:
        fail("config: missing or empty history")
    if not all(isinstance(h, dict) for h in history):
        fail("config: history entries must be JSON objects")
    non_empty_history = [h for h in history if h.get("empty_layer") is not True]
    if len(non_empty_history) != len(diff_ids):
        fail(
            f"config: {len(history)} history entries "
            f"({len(non_empty_history)} non-empty) do not match "
            f"{len(diff_ids)} layers"
        )

    arch = config.get("architecture")
    osname = config.get("os")
    if not isinstance(arch, str) or not arch:
        fail("config: missing architecture")
    if not isinstance(osname, str) or not osname:
        fail("config: missing os")

    prepared_tar_paths = []
    for i, ldesc in enumerate(layers):
        layer_no = i + 1  # 1-based manifest layer number
        if not isinstance(ldesc, dict):
            fail(f"layer {layer_no}: invalid descriptor")
        lhex = _parse_digest(ldesc.get("digest"), f"{prefix}layer {layer_no}")
        lpath = os.path.join(layout_dir, "blobs", "sha256", lhex)
        try:
            _verify_blob(lpath, ldesc, f"{prefix}layer {layer_no}")
        except UnpackError as e:
            fail(str(e))
        media_type = ldesc.get("mediaType")
        if media_type in TAR_MEDIA_TYPES:
            actual = _sha256_file(lpath)
            prepared_tar_paths.append(lpath if decompress_dir else None)
        elif media_type in GZIP_MEDIA_TYPES:
            if decompress_dir is not None:
                plain_path = os.path.join(decompress_dir, f"layer-{layer_no}.tar")
                try:
                    with open(plain_path, "wb") as dst:
                        actual = _stream_gunzip(lpath, dst)
                except UnpackError as e:
                    fail(f"layer {layer_no}: {e}")
                prepared_tar_paths.append(plain_path)
            else:
                try:
                    actual = _stream_gunzip(lpath)
                except UnpackError as e:
                    fail(f"layer {layer_no}: {e}")
                prepared_tar_paths.append(None)
        else:
            fail(f"layer {layer_no}: unsupported mediaType {media_type!r}")
            actual = None  # unreachable, fail() raises
        if actual != diff_ids[i]:
            fail(
                f"layer {layer_no}: diff_id mismatch "
                f"(config says sha256:{diff_ids[i]}, got sha256:{actual})"
            )

    return LayoutBundle(
        layout_dir=layout_dir,
        index=index,
        manifest_descriptor=mdesc,
        manifest=manifest,
        config_descriptor=cdesc,
        config=config,
        diff_ids=diff_ids,
        layer_descriptors=list(layers),
        prepared_tar_paths=prepared_tar_paths,
    )


def load_and_verify(layout_dir, workdir):
    """Verify the whole layout; return ordered list of plain layer tar paths.

    Gzip layers are decompressed once, in the same streaming pass that
    verifies their diff_id.
    """
    bundle = inspect_layout(layout_dir, decompress_dir=workdir)
    return [
        prepared or bundle.blob_path(desc["digest"])
        for prepared, desc in zip(
            bundle.prepared_tar_paths, bundle.layer_descriptors
        )
    ]


# ---------------------------------------------------------------------------
# path safety
# ---------------------------------------------------------------------------


def _resolve_within(root, relpath):
    """Resolve *relpath* against *root*, following existing symlinks.

    The returned absolute path is guaranteed to live inside *root*.
    The final component is never dereferenced (writes operate on it, not
    through it).  Raises UnpackError if the path would escape.
    """
    parts = []  # resolved components so far, relative to root
    todo = [p for p in relpath.split("/") if p not in ("", ".")]
    depth = 0
    while todo:
        comp = todo.pop(0)
        if comp == "..":
            if not parts:
                raise UnpackError(f"path escapes rootfs: {relpath!r}")
            parts.pop()
            continue
        candidate = os.path.join(root, *parts, comp)
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            parts.append(comp)
            continue
        except OSError as e:
            raise UnpackError(f"cannot inspect {relpath!r}: {e}")
        if stat.S_ISLNK(st.st_mode) and todo:
            depth += 1
            if depth > MAX_SYMLINK_DEPTH:
                raise UnpackError(f"too many symlinks resolving {relpath!r}")
            target = os.readlink(candidate)
            tparts = [p for p in target.split("/") if p not in ("", ".")]
            if target.startswith("/"):
                parts = []  # absolute symlink: relative to rootfs root
            todo = tparts + todo
        else:
            parts.append(comp)
    return os.path.join(root, *parts)


def _check_link_target(root, dest_dir_abs, linkname):
    """A symlink created in *dest_dir_abs* with target *linkname* must stay
    inside *root* (absolute targets are rootfs-relative, as in containers)."""
    if not isinstance(linkname, str) or not linkname or "\x00" in linkname:
        raise UnpackError(f"invalid symlink target: {linkname!r}")
    if linkname.startswith("/"):
        parts = []
    else:
        rel = os.path.relpath(dest_dir_abs, root)
        parts = [] if rel == "." else rel.split(os.sep)
    for p in linkname.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if not parts:
                raise UnpackError(f"symlink target escapes rootfs: {linkname!r}")
            parts.pop()
        else:
            parts.append(p)


# ---------------------------------------------------------------------------
# filesystem mutation helpers
# ---------------------------------------------------------------------------


def _remove_existing(path):
    """Delete *path* (file/symlink/dir tree) if it exists."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _drop_dir_meta(dir_meta, path, include_self):
    prefix = path + os.sep
    for key in [k for k in dir_meta if k.startswith(prefix) or (include_self and k == path)]:
        dir_meta.pop(key, None)


def _apply_meta(path, member, is_link):
    if not is_link:
        os.chmod(path, member.mode & 0o7777)
    if os.geteuid() == 0:
        try:
            os.chown(path, member.uid, member.gid, follow_symlinks=False)
        except OSError:
            pass
    try:
        os.utime(path, (member.mtime, member.mtime), follow_symlinks=False)
    except (OSError, ValueError):
        pass


def _apply_entry(tf, member, rel, rootfs, dir_meta):
    dest = _resolve_within(rootfs, rel)
    parent = os.path.dirname(dest)

    if member.isdir():
        if os.path.islink(dest) or (os.path.lexists(dest) and not os.path.isdir(dest)):
            _remove_existing(dest)
        os.makedirs(dest, exist_ok=True)
        # Directory metadata is applied only after all layers are unpacked,
        # so restrictive modes cannot break writes into this directory and
        # later entries still update its mtime.
        dir_meta[dest] = (member.mode & 0o7777, member.mtime, member.uid, member.gid)
        return

    os.makedirs(parent, exist_ok=True)

    if member.isreg():
        src = tf.extractfile(member)
        if src is None:
            raise UnpackError(f"cannot read file entry {rel!r}")
        if os.path.isdir(dest) and not os.path.islink(dest):
            _remove_existing(dest)
        # Write to a temp file and rename: replacing one end of a hardlink
        # pair must leave the other end's bytes untouched.
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".unpack-")
        try:
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
            os.replace(tmp, dest)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        _apply_meta(dest, member, is_link=False)
    elif member.issym():
        _check_link_target(rootfs, parent, member.linkname)
        _remove_existing(dest)
        os.symlink(member.linkname, dest)
        _apply_meta(dest, member, is_link=True)
    elif member.islnk():
        tparts = _normalize_archive_name(member.linkname, f"hardlink target of {rel!r}")
        if not tparts:
            raise UnpackError(f"hardlink {rel!r}: empty target")
        src = _resolve_within(rootfs, "/".join(tparts))
        if not os.path.lexists(src):
            raise UnpackError(
                f"hardlink {rel!r}: target {member.linkname!r} not found in rootfs"
            )
        _remove_existing(dest)
        try:
            os.link(src, dest, follow_symlinks=False)
        except OSError as e:
            raise UnpackError(f"hardlink {rel!r}: {e}")
        _apply_meta(dest, member, is_link=False)
    elif member.isfifo():
        _remove_existing(dest)
        os.mkfifo(dest, member.mode & 0o7777)
        _apply_meta(dest, member, is_link=False)
    elif member.ischr() or member.isblk():
        _remove_existing(dest)
        mode = (stat.S_IFCHR if member.ischr() else stat.S_IFBLK) | (member.mode & 0o7777)
        try:
            os.mknod(dest, mode, os.makedev(member.devmajor, member.devminor))
        except PermissionError:
            return  # device nodes need privileges; skip when unprivileged
        _apply_meta(dest, member, is_link=False)
    else:
        raise UnpackError(f"unsupported entry type {member.type!r} for {rel!r}")


# ---------------------------------------------------------------------------
# layer application (whiteout / opaque handling)
# ---------------------------------------------------------------------------


def apply_layer(tar_path, rootfs, dir_meta, layer_number):
    """Apply one plain layer tar onto *rootfs*.

    *layer_number* is the 1-based position of the layer in the image
    manifest; every entry-level failure is raised as
    :class:`LayerApplicationError` carrying that number and the entry path.
    """
    try:
        tf = tarfile.open(tar_path, "r:")
    except (tarfile.TarError, OSError) as e:
        raise UnpackError(f"layer {layer_number}: corrupt layer tar: {e}")
    with tf:
        try:
            members = tf.getmembers()
        except tarfile.TarError as e:
            raise UnpackError(f"layer {layer_number}: corrupt layer tar: {e}")

        entries = []
        deletions = []
        opaque_dirs = []
        for m in members:
            try:
                parts = _normalize_archive_name(m.name, "layer entry")
            except UnpackError as e:
                raise LayerApplicationError(layer_number, m.name, str(e))
            if not parts:
                continue
            base = parts[-1]
            rel = "/".join(parts)
            if base == OPAQUE_MARKER:
                opaque_dirs.append((parts[:-1], rel))
            elif base.startswith(WHITEOUT_PREFIX):
                target_name = base[len(WHITEOUT_PREFIX):]
                if not target_name:
                    raise LayerApplicationError(
                        layer_number, rel, "invalid whiteout entry"
                    )
                deletions.append((parts[:-1] + [target_name], rel))
            else:
                entries.append((m, parts, rel))

        # Phase 1: deletions apply to the *lower* filesystem state only.
        # They are all executed before anything from this layer is written,
        # so a marker can never delete same-layer content regardless of its
        # position inside the layer tar.
        for target, marker_rel in deletions:
            try:
                p = _resolve_within(rootfs, "/".join(target))
                _remove_existing(p)
                _drop_dir_meta(dir_meta, p, include_self=True)
            except UnpackError as e:
                raise LayerApplicationError(layer_number, marker_rel, str(e))
        for d, marker_rel in opaque_dirs:
            try:
                p = _resolve_within(rootfs, "/".join(d)) if d else rootfs
                if os.path.isdir(p):
                    for child in os.listdir(p):
                        _remove_existing(os.path.join(p, child))
                _drop_dir_meta(dir_meta, p, include_self=False)
            except UnpackError as e:
                raise LayerApplicationError(layer_number, marker_rel, str(e))

        # Phase 2: write this layer's new content in tar order.
        for m, parts, rel in entries:
            try:
                _apply_entry(tf, m, rel, rootfs, dir_meta)
            except LayerApplicationError:
                raise
            except UnpackError as e:
                raise LayerApplicationError(layer_number, rel, str(e))
            except (OSError, ValueError, tarfile.TarError) as e:
                raise LayerApplicationError(
                    layer_number, rel, f"failed to apply entry: {e}"
                )


def _apply_deferred_dir_meta(dir_meta):
    # Deepest directories first so a restrictive parent mode cannot lock us
    # out of still-pending children.
    for path in sorted(dir_meta, key=lambda p: -p.count(os.sep)):
        mode, mtime, uid, gid = dir_meta[path]
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        if os.geteuid() == 0:
            try:
                os.chown(path, uid, gid)
            except OSError:
                pass
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        try:
            os.utime(path, (mtime, mtime))
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
# rootfs.tar output
# ---------------------------------------------------------------------------


def _tar_add(tf, rootfs, rel, inode_map):
    full = os.path.join(rootfs, rel)
    st = os.lstat(full)
    ti = tarfile.TarInfo(rel)
    ti.mode = stat.S_IMODE(st.st_mode)
    ti.mtime = int(st.st_mtime)
    ti.uid = st.st_uid
    ti.gid = st.st_gid
    if stat.S_ISDIR(st.st_mode):
        ti.type = tarfile.DIRTYPE
        tf.addfile(ti)
        for child in sorted(os.listdir(full)):
            _tar_add(tf, rootfs, rel + "/" + child, inode_map)
    elif stat.S_ISLNK(st.st_mode):
        ti.type = tarfile.SYMTYPE
        ti.linkname = os.readlink(full)
        tf.addfile(ti)
    elif stat.S_ISREG(st.st_mode):
        key = (st.st_dev, st.st_ino)
        if st.st_nlink > 1 and key in inode_map:
            ti.type = tarfile.LNKTYPE
            ti.linkname = inode_map[key]
            tf.addfile(ti)
        else:
            if st.st_nlink > 1:
                inode_map[key] = rel
            ti.type = tarfile.REGTYPE
            ti.size = st.st_size
            with open(full, "rb") as f:
                tf.addfile(ti, f)
    elif stat.S_ISFIFO(st.st_mode):
        ti.type = tarfile.FIFOTYPE
        tf.addfile(ti)
    elif stat.S_ISCHR(st.st_mode):
        ti.type = tarfile.CHRTYPE
        ti.devmajor = os.major(st.st_rdev)
        ti.devminor = os.minor(st.st_rdev)
        tf.addfile(ti)
    elif stat.S_ISBLK(st.st_mode):
        ti.type = tarfile.BLKTYPE
        ti.devmajor = os.major(st.st_rdev)
        ti.devminor = os.minor(st.st_rdev)
        tf.addfile(ti)
    else:
        raise UnpackError(f"unsupported file type in rootfs: {rel}")


def build_rootfs_tar(rootfs, out_path):
    tmp = out_path + ".tmp"
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tf:
        for child in sorted(os.listdir(rootfs)):
            _tar_add(tf, rootfs, child, {})
    os.replace(tmp, out_path)


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------


def unpack_layout_dir(layout_dir, workdir):
    """Verify and unpack an already-extracted layout directory."""
    layer_tars = load_and_verify(layout_dir, workdir)

    rootfs = os.path.join(workdir, "rootfs")
    os.makedirs(rootfs)
    dir_meta = {}
    for i, tar_path in enumerate(layer_tars, start=1):
        apply_layer(tar_path, rootfs, dir_meta, i)
    _apply_deferred_dir_meta(dir_meta)

    out_path = os.path.join(workdir, "rootfs.tar")
    build_rootfs_tar(rootfs, out_path)
    return out_path


def unpack_image(upload_path, workdir):
    """Unpack the uploaded layout tar; return the path of the rootfs.tar.

    Raises UnpackError on any problem; nothing is returned in that case.
    """
    layout_dir = os.path.join(workdir, "layout")
    os.makedirs(layout_dir)
    extract_layout(upload_path, layout_dir)
    return unpack_layout_dir(layout_dir, workdir)
