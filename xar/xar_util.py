from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections
import errno
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import time
import struct
import sys
import uuid
import zipfile

logger = logging.getLogger('xar')


PYTHON_EXTS = [".py", ".pyc"]


def make_uuid():
    # ugh line length limit; we need a small uuid
    return str(uuid.uuid1()).split("-")[0]


def _align_offset(offset, align=4096):
    """Aligns the offset to the given alignment"""
    mask = align - 1
    assert (mask & align) == 0
    return (offset + mask) & (~mask)


class SquashfsOptions(object):
    def __init__(self):
        self.compression_algorithm = "zstd"
        self.zstd_level = 16
        self.block_size = 256 * 1024


class XarFactory(object):
    """A class for creating XAR files.

    Pretty straight forward; take an input directory, output file, and some
    metadata and produce a XAR file of the contents.
    """
    def __init__(self, dirname, output, header_prefix):
        self.dirname = dirname
        self.output = output
        self.header_prefix = header_prefix
        self.xar_header = {}
        self.uuid = None
        self.version = None
        self.sort_file = None
        self.squashfs_options = SquashfsOptions()

    def go(self):
        "Make the XAR file."
        logger.info("Squashing %s to %s" % (self.dirname, self.output))
        if self.uuid is None:
            self.uuid = make_uuid()

        if self.version is None:
            self.version = time.time()

        tf = tempfile.NamedTemporaryFile(delete=False)
        # Create!
        sqopts = self.squashfs_options
        cmd = ["mksquashfs", self.dirname, tf.name, "-noappend",
               '-noI', '-noX',  # is this worth it?  probably
               '-force-uid', 'nobody',
               '-force-gid', 'nobody',
               '-b', str(sqopts.block_size),
               "-comp", sqopts.compression_algorithm]
        if sqopts.compression_algorithm == 'zstd':
            cmd.extend(('-Xcompression-level', str(sqopts.zstd_level)))

        if self.sort_file:
            cmd.extend(['-sort', self.sort_file])

        if sys.stdout.isatty():
            subprocess.check_call(cmd)
        else:
            with open("/dev/null", "wb") as f:
                subprocess.check_call(cmd, stdout=f)

        headers = [self.header_prefix]
        # Take the squash file, create a header, and write it
        with open(self.output, "wb") as of:
            # Make a "safe" header that is easily parsed and also not
            # going to explode if accidentally executed.
            headers.append('OFFSET="$OFFSET"')
            headers.append('UUID="$UUID"')
            headers.append('VERSION="%d"' % self.version)
            for key, val in self.xar_header.items():
                headers.append('%s="%s"' % (key, str(val).replace('"', ' ')))
            headers.append("#xar_stop")
            headers.append("echo This XAR file should not be executed by sh")
            headers.append("exit 1")
            headers.append("# Actual squashfs file begins at $OFFSET")
            text_headers = "\n".join(headers) + '\n'
            # 128 is to account for expansion of $OFFSET and $UUID;
            # it's well over what they might reasonably be.
            header_size = _align_offset(128 + len(text_headers))
            text_headers = text_headers.replace("$OFFSET", "%d" % header_size)
            text_headers = text_headers.replace("$UUID", self.uuid)
            text_headers += '\n' * (header_size - len(text_headers))
            of.write(text_headers.encode('UTF-8'))

            # Now append the squashfs file to the header.
            with open(tf.name, "rb") as rf:
                while True:
                    data = rf.read(1024 * 1024)
                    if not data:
                        break
                    of.write(data)


def safe_mkdir(directory):
    try:
        os.makedirs(directory)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise


def safe_remove(filename):
    try:
        os.unlink(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def safe_rmtree(directory):
    if os.path.exists(directory):
        shutil.rmtree(directory, True)


# Simplified version of Chroot from PEX
class StagingDirectory(object):
    """
    Manages the staging directory.
    """
    class Error(Exception):
        pass

    def __init__(self, staging_dir=None):
        self._staging = staging_dir or tempfile.mkdtemp()
        safe_mkdir(self._staging)

    def _normalize(self, dst):
        dst = os.path.normpath(dst)
        if dst.startswith(os.sep) or dst.startswith('..'):
            raise self.Error("Destination path '%s' is not a relative!" % dst)
        return dst

    def _ensure_parent(self, dst):
        safe_mkdir(os.path.dirname(self.absolute(dst)))

    def _ensure_not_dst(self, dst):
        if self.exists(dst):
            raise self.Error("Destination path '%s' already exists!" % dst)

    def clone(self, staging_dir=None):
        """Clone the staging directory"""
        dst = StagingDirectory(staging_dir or tempfile.mkdtemp())
        dst.copytree(self._staging)
        return dst

    def path(self):
        """Returns the root directory of the staging directory."""
        return self._staging

    def absolute(self, dst):
        """Returns absolute path for a path relative to staging directory."""
        dst = self._normalize(dst)
        return os.path.join(self._staging, dst)

    def delete(self):
        """Delete the staging directory."""
        safe_rmtree(self._staging)

    def copy(self, src, dst):
        """Copy src into dst under the staging directory."""
        dst = self._normalize(dst)
        self._ensure_parent(dst)
        self._ensure_not_dst(dst)
        shutil.copy2(src, self.absolute(dst))

    def write(self, data, dst, mode, permissions):
        """Write data into dst."""
        dst = self._normalize(dst)
        self._ensure_parent(dst)
        self._ensure_not_dst(dst)
        with open(self.absolute(dst), mode) as f:
            f.write(data)
        os.chmod(self.absolute(dst), permissions)

    def _resolve_dst_dir(self, dst):
        if dst is None:
            # Replace the current staging directory
            if os.listdir(self._staging) != []:
                raise self.Error("Staging directory is not empty!")
            # shutil requires that the destination directory does not exist
            safe_rmtree(self._staging)
            dst = '.'
        dst = self._normalize(dst)
        self._ensure_not_dst(dst)
        return dst

    def copytree(self, src, dst=None):
        """Copy src dir into dst under the staging directory."""
        dst = self._resolve_dst_dir(dst)
        shutil.copytree(src, self.absolute(dst))

    def symlink(self, link, dst):
        """Write symbolic link to dst under the staging directory."""
        dst = self._normalize(dst)
        self._ensure_parent(dst)
        self._ensure_not_dst(dst)
        os.symlink(link, self.absolute(dst))

    def move(self, src, dst):
        """Move src into dst under the staging directory."""
        dst = self._normalize(dst)
        self._ensure_parent(dst)
        self._ensure_not_dst(dst)
        shutil.move(src, self.absolute(dst))

    def exists(self, dst):
        """Checks if dst exists under the staging directory."""
        dst = self._normalize(dst)
        return os.path.exists(self.absolute(dst))

    def extract(self, zf, dst=None):
        """Extracts the zipfile into dst under the staging directory."""
        dst = self._resolve_dst_dir(dst)
        abs_dst = os.path.join(self._staging, dst)
        timestamps = {}
        for zi in zf.infolist():
            filename = os.path.join(dst, zi.filename)
            destination = self.absolute(filename)

            mode = zi.external_attr >> 16
            if stat.S_ISLNK(mode):
                target = zf.read(zi).decode("utf-8")
                self.symlink(target, filename)
            else:
                self._ensure_parent(filename)
                zf.extract(zi, path=abs_dst)
                os.chmod(destination, stat.S_IMODE(mode))

            # Use the embedded timestamp for from the pyc file for the
            # pyc and py file; otherwise, use the timezone-less
            # timestamp from the zipfile (sigh).
            if filename.endswith(".pyc"):
                new_time = extract_pyc_timestamp(destination)
                timestamps[destination] = new_time       # pyc file
                timestamps[destination[:-1]] = new_time  # py file too
            else:
                new_time = tuple((list(zi.date_time) + [0, 0, -1]))
                timestamps[destination] = time.mktime(new_time)

        # Set our timestamps.
        for path, timestamp in timestamps.items():
            try:
                os.utime(path, (timestamp, timestamp))
            except OSError as e:
                # Sometimes we had a pyc file but no py file; the utime
                # would fail.
                if not path.endswith(".py"):
                    raise e


# Simple class to represent a partition destination.  Each destination
# is a path and a uuid from which the contents come (ie, the uuid of
# the spar file that contains the file that is moved into the
# partition; used for symlink construction).
PartitionDestination = collections.namedtuple(
    'PartitionDestination', 'staging uuid')


def partition_files(staging, extension_destinations):
    """Partition source_dir into multiple output directories.

    A partition is defined by extension_destinations which maps suffixes (such
    as ".debuginfo") to a PartitionDestination instance.

    dest_dir contains all files that aren't in a partition, and symlinks for
    ones that are.  symlinks are relative and of the form
    "../../../uuid/path/to/file" so that the final symlinks are correct
    relative to /mnt/xar/....
    """
    source_dir = staging.path()
    source_dir = source_dir.rstrip('/')

    for dirpath, _dirnames, filenames in os.walk(staging.path()):
        # path relative to source_dir; used for creating the right
        # file inside the staging dir
        relative_dirname = dirpath[len(source_dir) + 1:]

        # Special case; if a file is in the root of source_dir, then
        # relative_dirname is empty, but that has the same number of
        # '/' as just 'bin', so we need to special case it the empty
        # value.
        if not relative_dirname:
            relative_depth = 1
        else:
            relative_depth = 2 + relative_dirname.count("/")

        for filename in filenames:
            # Does this extension map to a separate output?
            _, extension = os.path.splitext(filename)
            dest_base = extension_destinations.get(extension, None)
            # This path stays in the source staging directory
            if dest_base is None:
                continue
            # This file is destined for another tree, make a
            # relative symlink in source pointing to the
            # sub-xar destination.
            relative_path = os.path.join(relative_dirname, filename)
            source_path = staging.absolute(relative_path)
            dest_base.staging.move(source_path, relative_path)

            dependency_mountpoint = dest_base.uuid
            staging_symlink = os.path.join(
                '../' * relative_depth,
                dependency_mountpoint,
                relative_path)
            logging.info("%s %s" % (staging_symlink, source_path))

            staging.symlink(staging_symlink, relative_path)


def write_sort_file(staging_dir, extension_priorities, sort_file):
    """
    Write a sort file for mksquashfs to colocate some files at the beginning.
    Files are assigned priority by extension, with files earlier in the list
    appearing first. The result is written to the file object sort_file.
    mksquashfs takes the sort file with the option '-sort sort_filename'.
    """
    for dirpath, _dirname, filenames in os.walk(staging_dir):
        for filename in filenames:
            fn = os.path.join(dirpath, filename)
            for idx, suffix in enumerate(extension_priorities):
                if fn.endswith(suffix):
                    # Default priority is 0; make ours all
                    # negative so we can not list files with
                    # spaces in the name, making them default
                    # to 0
                    priority = idx - len(extension_priorities) - 1
                    break

            assert fn.startswith(staging_dir + '/')
            fn = fn[len(staging_dir) + 1:]

            # Older versions of mksquashfs don't like spaces
            # in filenames; let them have the default priority
            # of 0.
            if ' ' not in fn:
                sort_file.write("%s %d\n" % (fn, priority))


def extract_pyc_timestamp(path):
    "Extract the embedded timestamp from a pyc file"

    # A PYC file has a four byte header then four byte timestamp.  The
    # timestamp must match the timestamp on the py file, otherwise the
    # interpreter will attempt to re-compile the py file.  We extract
    # the timestamp to adulterate the py/pyc files before squashing
    # them.
    with open(path, "rb") as fh:
        prefix = fh.read(8)
        return struct.unpack(b'<I', prefix[4:])[0]


def parse_entry_point(entry_point):
    """
    Parses a Python entry point and returns the module and function.
    The two allowed formats are 'path.to.module', and 'path.to.module:function'.
    In the former case, ('path.to.module', None) is returned.
    In the latter case, ('path.to.module', 'function') is returned.
    """
    module, sep, function = entry_point.partition(':')
    if function and sep and module:
        return (module, function)
    else:
        return (module, None)


def file_in_zip(zf, filename):
    """Returns True if :filename: is present in the zipfile :zf:."""
    try:
        zf.getinfo(filename)
        return True
    except KeyError:
        return False


def extract_python_archive_info(archive):
    """
    Extracts the shebang (if any) from a python archive, along with the entry
    point (if any). Returns a tuple (python_interpreter, entry_point).
    Avoids interpreting the shebang in it doesn't contain 'python'.
    """
    python = None
    with open(archive, "rb") as f:
        if f.read(2) == b"#!":
            shebang = f.readline().decode("utf-8").strip()
            if "python" in shebang:
                python = shebang
    with zipfile.ZipFile(archive) as zf:
        MAIN = "__main__"
        main_exists = any(file_in_zip(zf, MAIN + ext) for ext in PYTHON_EXTS)
        if main_exists:
            return (python, MAIN)
        return (python, None)


def get_python_main(directory):
    """Returns the python __main__ from a directory (if it exists)."""
    main_exists = any(os.path.exists("__main__" + ext) for ext in PYTHON_EXTS)
    if main_exists:
        return "__main__"
    return None
