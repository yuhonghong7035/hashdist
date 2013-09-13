"""
:mod:`hashdist.core.source_cache` --- Source cache
==================================================

The source cache makes sure that one doesn't have to re-download source code
from the net every time one wants to rebuild. For consistency/simplicity, the
software builder also requires that local sources are first "uploaded" to the
cache.

The software cache currently has explicit support for tarballs, git,
and storing files as-is without metadata. A "source item" (tarball, git commit, or set
of files) is identified by a secure hash. The generic API in :meth:`SourceCache.fetch` and
:meth:`SourceCache.unpack` works by using such hashes as keys. The retrieval
and unpacking methods are determined by the key prefix::

    sc.fetch('http://python.org/ftp/python/2.7.3/Python-2.7.3.tar.bz2',
             'tar.bz2:ttjyphyfwphjdc563imtvhnn4x4pluh5')
    sc.unpack('tar.bz2:ttjyphyfwphjdc563imtvhnn4x4pluh5', '/your/location/here')

    sc.fetch('https://github.com/numpy/numpy.git',
             'git:35dc14b0a59cf16be8ebdac04f7269ac455d5e43')

For cases where one doesn't know the key up front one uses the
key-retrieving API. This is typically done in interactive settings to
aid distribution/package developers::

    key1 = sc.fetch_git('https://github.com/numpy/numpy.git', 'master')
    key2 = sc.fetch_archive('http://python.org/ftp/python/2.7.3/Python-2.7.3.tar.bz2')

Features
--------

 * Re-downloading all the sources on each build gets old quickly...

 * Native support for multiple retrieval mechanisms. This is important as
   one wants to use tarballs for slowly-changing stable code, but VCS for
   quickly-changing code.

 * Isolates dealing with various source code retrieval mechanisms from
   upper layers, who can simply pass along two strings regardless of method.

 * Safety: Hashes are re-checked on the fly while unpacking, to protect
   against corruption or tainting of the source cache.

 * Should be safe for multiple users to share a source cache directory
   on a shared file-system as long as all have write access, though this
   may need some work with permissions.


Source keys
-----------

The keys for a given source item can be determined
*a priori*. The rules are as follows:

Tarballs/archives:
    SHA-256, encoded in base64 using :func:`.format_digest`. The prefix
    is currently either ``tar.gz`` or ``tar.bz2``.

Git commits:
    Identified by their (SHA-1) commits prefixed with ``git:``.

Individual files or directories ("hit-pack"):
    A tarball hash is not deterministic from the file
    contents alone (there's metadata, compression, etc.). In order to
    hash build scripts etc. with hashes based on the contents alone, we
    use a custom "archive format" as the basis of the hash stream.
    The format starts with the 8-byte magic string "HDSTPCK1",
    followed by each file sorted by their filename (potentially
    containing "/"). Each file is stored as

    ==========================  ==============================
    little-endian ``uint32_t``  length of filename
    little-endian ``uint32_t``  length of contents
    ---                         filename (no terminating null)
    ---                         contents
    ==========================  ==============================

    This stream is then encoded like archives (SHA-256 in base-64),
    and prefixed with ``files:`` to get the key.

Module reference
----------------

"""

import os
import re
import sys
import subprocess
import tempfile
import urllib2
import json
import shutil
import hashlib
import struct
import errno
import stat
from timeit import default_timer as clock
from contextlib import closing

from .hasher import Hasher, format_digest, HashingReadStream, HashingWriteStream
from .fileutils import silent_makedirs

pjoin = os.path.join

TAG_RE_S = r'^[a-zA-Z-_+=]+$'
TAG_RE = re.compile(TAG_RE_S)

PACKS_DIRNAME = 'packs'
GIT_DIRNAME = 'git'

class SourceCacheError(Exception):
    pass

class SourceNotFoundError(SourceCacheError):
    pass

class KeyNotFoundError(SourceCacheError):
    pass

class CorruptSourceCacheError(SourceCacheError):
    pass

class SecurityError(SourceCacheError):
    pass

class ProgressBar(object):

    def __init__(self, total_size, bar_length=25):
        """
        total_size ... the size in bytes of the file to be downloaded
        """
        self._total_size = total_size
        self._bar_length = bar_length
        self._t1 = clock()

    def update(self, current_size):
        """
        actual_size ... the current size of the downloading file
        """
        time_delta = clock() - self._t1
        f1 = self._bar_length * current_size / self._total_size
        f2 = self._bar_length - f1
        percent = 100. * current_size / self._total_size
        if time_delta == 0:
            rate_eta_str = ""
        else:
            rate = 1. * current_size / time_delta # in bytes / second
            eta = (self._total_size-current_size) / rate # in seconds
            rate_eta_str = "%.3fMB/s ETA " % (rate / 1024.**2)
            if eta < 70:
                rate_eta_str += "%ds" % (int(eta))
            else:
                minutes = int(eta / 60)
                seconds = eta - minutes * 60
                rate_eta_str += "%dmin %ds" % (minutes, seconds)
        msg = "\r[" + "="*f1 + " "*f2 + "] %4.1f%% (%.1fMB of %.1fMB) %s  " % \
                (percent, current_size / 1024.**2, self._total_size / 1024.**2,
                        rate_eta_str)
        sys.stdout.write(msg)
        sys.stdout.flush()

    def finish(self):
        sys.stdout.write("\n")

def single_file_key(filename, contents):
    h = Hasher()
    h.update('file')
    h.update({'filename': filename,
              'contents': contents})
    return 'file:' + h.format_digest()


def mkdir_if_not_exists(path):
    try:
        os.mkdir(path)
    except OSError, e:
        if e.errno != errno.EEXIST:
            raise

class SourceCache(object):
    """
    """

    def __init__(self, cache_path, logger, mirrors=(), create_dirs=False):
        if not os.path.isdir(cache_path):
            if create_dirs:
                silent_makedirs(cache_path)
            else:
                raise ValueError('"%s" is not an existing directory' % cache_path)
        self.cache_path = os.path.realpath(cache_path)
        self.logger = logger
        self.mirrors = mirrors

    def _ensure_subdir(self, name):
        path = pjoin(self.cache_path, name)
        mkdir_if_not_exists(path)
        return path

    def delete_all(self):
        shutil.rmtree(self.cache_path)
        os.mkdir(self.cache_path)

    @staticmethod
    def create_from_config(config, logger, create_dirs=False):
        """Creates a SourceCache from the settings in the configuration
        """
        mirror = config['sourcecache/mirror'].strip()
        mirrors = [mirror] if mirror else []
        return SourceCache(config['sourcecache/sources'], logger, mirrors, create_dirs)

    def fetch_git(self, repository, rev, repo_name):
        """Fetches source code from git repository

        With this method one does not need to know a specific commit,
        but can use a generic git rev such as `master` or
        `revs/heads/master`.  In automated settings or if the commit
        hash is known exactly, :meth:`fetch` should be used instead.

        Parameters
        ----------

        repository : str
            The repository URL (forwarded to git)

        rev : str
            The rev to download (forwarded to git)

        repo_name : str
            A unique name to use for the repository, e.g., ``numpy``.
            This is currently required because git doesn't seem to allow
            getting a unique ID for a remote repo; and cloning all repos
            into the same git repo has scalability issues.


        Returns
        -------

        key : str
            The globally unique key; this is the git commit SHA-1 hash
            prepended by ``git:``.
        
        """
        return GitSourceCache(self).fetch_git(repository, rev, repo_name)

    def fetch_archive(self, url, type=None):
        """Fetches  a tarball without knowing the key up-front.

        In automated settings, :meth:`fetch` should be used instead.

        Parameters
        ----------

        url : str
            Where to download archive from. Local files can be specified
            by prepending ``"file:"`` to the path.

        type : str (optional)
            Type of archive, such as ``"tar.gz"``, ``"tar.gz2"``. For use
            when this cannot be determined from the suffix of the url.
        
        """
        return ArchiveSourceCache(self).fetch_archive(url, type, None)


    def put(self, files):
        """Put in-memory contents into the source cache.

        Parameters
        ----------
        files : dict or list of (filename, contents)
            The contents of the archive. `filename` may contain forward
            slashes ``/`` as path separators. `contents` is a pure bytes
            objects which will be dumped directly to `stream`.

        Returns
        -------

        key : str
            The resulting key, it has the ``files:`` prefix.

        """
        return ArchiveSourceCache(self).put(files)

    def _get_handler(self, type):
        if type == 'git':
            handler = GitSourceCache(self)
        elif type == 'files' or type in archive_types:
            handler = ArchiveSourceCache(self)
        else:
            raise ValueError('does not recognize key prefix: %s' % type)
        return handler

    def fetch(self, url, key, repo_name=None):
        """Fetch sources whose key is known.

        This is the method to use in automated settings. If the
        sources globally identified by `key` are already present in
        the cache, the method returns immediately, otherwise it
        attempts to download the sources from `url`. How to interpret
        the URL is determined by the prefix of `key`.

        Parameters
        ----------

        url : str or None
            Location to download sources from. Exact meaning depends on
            prefix of `key`. If `None` is passed, an exception is raised
            if the source object is not present.

        key : str
            Globally unique key for the source object.

        repo_name : str or None
            A unique ID for the source code repo; required for git and ignored
            otherwise. This must be present because a git "project" is distributed
            and cannot be deduced from URL (and pulling everything into the same
            repo was way too slow). Hopefully this can be mended in the future.
        """
        type, hash = key.split(':')
        handler = self._get_handler(type)
        handler.fetch(url, type, hash, repo_name)

    def unpack(self, key, target_path):
        """
        Unpacks the sources identified by `key` to `target_path`

        The sources are verified against their secure hash to guard
        against corruption/security problems. `CorruptSourceCacheError`
        will be raised in this case. In normal circumstances this should
        never happen.

        The archive will be loaded into memory, checked against the
        hash, and then extracted from the memory copy, so that attacks
        through tampering with on-disk archives should not be possible.

        Parameters
        ----------

        key : str
            The source item key/secure hash

        target_path : str
            Path to extract in

        """
        if not os.path.exists(target_path):
            os.makedirs(target_path)
        if not ':' in key:
            raise ValueError("Key must be on form 'type:hash'")
        type, hash = key.split(':')
        handler = self._get_handler(type)
        handler.unpack(type, hash, target_path)


class GitSourceCache(object):
    # Group together methods for working with the part of the source
    # cache stored with git.

    def __init__(self, source_cache):
        self.repo_path = pjoin(source_cache.cache_path, GIT_DIRNAME)
        self.logger = source_cache.logger

    def git_interactive(self, repo_name, *args):
        # Inherit stdin/stdout in order to interact with user about any passwords
        # required to connect to any servers and so on
        if args[0] == 'init':
            env = os.environ
        else:
            env = self.get_repo_env(repo_name)
        subprocess.check_call(['git'] + list(args), env=env)

    def git(self, repo_name, *args):
        p = subprocess.Popen(['git'] + list(args), env=self.get_repo_env(repo_name),
                             stdout=subprocess.PIPE, stdin=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        out, err = p.communicate()
        return p.returncode, out, err

    def checked_git(self, repo_name, *args):
        retcode, out, err = self.git(repo_name, *args)
        # Just fetch the output
        if retcode != 0:
            raise RuntimeError('git call %r failed with code %d' % (args, retcode))
        return out

    def get_repo_env(self, repo_name):
        repo_path = pjoin(self.repo_path, repo_name)
        if not os.path.exists(repo_path):
            # TODO: This is not race-safe
            os.makedirs(repo_path)
            self.checked_git(repo_name, 'init', '--bare', '-q', repo_path)
        env = dict(os.environ)
        env['GIT_DIR'] = repo_path
        return env

    def _resolve_remote_rev(self, repo_name, repo_url, rev):
        # Resolve the rev (if it is a branch/tag) to a commit hash
        p = self.checked_git(repo_name, 'ls-remote', repo_url, rev)
        lines = str(p).splitlines()
        if len(lines) == 0:
            msg = "no remote head '%s' found in git repo %s" % (rev, repo_url)
            self.logger.error(msg)
            raise SourceNotFoundError(msg)
        elif len(lines) == 1:
            # Use the hash for the rev instead
            commit = lines[0].split('\t')[0]
        else:
            msg = '"%s" resolves to multiple heads in "%s"' % (rev, repo_url)
            self.logger.error(msg + ':')
            for line in lines:
                self.logger.error(line.replace('\t', '    '))
            self.logger.error('Please specify the head by full name')
            raise SourceNotFoundError(msg)
        return commit

    def _does_branch_exist(self, repo_name, branch):
        retcode, out, err = self.git(repo_name,
                                     'show-ref', '--verify', '--quiet', 'refs/heads/%s' % branch)
        return retcode == 0        

    def _mark_commit_as_in_use(self, repo_name, commit):
        retcode, out, err = self.git(repo_name, 'branch', 'inuse/%s' % commit, commit)
        if retcode != 0:
            # Did it already exist? If so we're good (except if hashdist gc runs
            # at the same time...)
            if not self._does_branch_exist(repo_name, 'inuse/%s' % commit):
                raise RuntimeError('git branch failed with code %d: %s' % (retcode, err))

    def fetch(self, url, type, commit, repo_name):
        assert type == 'git'
        if repo_name is None:
            raise TypeError('Need to provide repo_name when fetching git archive')
        retcode, out, err = self.git(repo_name, 'rev-list', '-n1', '--quiet', commit)
        if retcode == 0:
            self._mark_commit_as_in_use(repo_name, commit)
        elif url is None:
            raise SourceNotFoundError('git:%s not present and repo url not provided' % commit)
        else:
            terms = url.split(' ')
            if len(terms) == 1:
                repo, = terms
                branch = None
            elif len(terms) == 2:
                repo, branch = terms
            else:
                raise ValueError('Please specify git repository as "git://repo/url [branchname]"')
            self.fetch_git(repo, branch, repo_name, commit)

    def _has_commit(self, repo_name, commit):
        # Assert that the commit is indeed present and is a commit hash and not a revspec
        retcode, out, err = self.git(repo_name, 'rev-list', '-n1', '--quiet', commit)
        return retcode == 0

    def fetch_git(self, repo_url, rev, repo_name, commit=None):
        if commit is None and rev is None:
            raise ValueError('Either a commit or a branch/rev must be specified')
        elif commit is None:
            # It is important to resolve the rev remotely, we can't trust local
            # branch-names at all since we merge all projects encountered into the
            # same repo
            commit = self._resolve_remote_rev(repo_name, repo_url, rev)
            
        if rev is not None:
            try:
                self.git_interactive(repo_name, 'fetch', repo_url, rev)
            except subprocess.CalledProcessError:
                self.logger.error('failed command: git fetch %s %s' % (repo_url, rev))
                raise
        else:
            # when rev is None, fetch all the remote heads; seems like one must
            # do a separate ls-remote...
            out = self.checked_git(repo_name, 'ls-remote', repo_url)
            heads = [line.split()[1] for line in out.splitlines() if line.strip()]
            # Fix for https://github.com/hashdist/python-hpcmp2/issues/57
            heads = [x for x in heads if not x.endswith("^{}")]
            self.git_interactive(repo_name, 'fetch', repo_url, *heads)
            
        if not self._has_commit(repo_name, commit):
            raise SourceNotFoundError('Repository "%s" did not contain commit "%s"' %
                                      (repo_url, commit))

        # Create a branch so that 'git gc' doesn't collect it
        self._mark_commit_as_in_use(repo_name, commit)

        return 'git:%s' % commit

    def unpack(self, type, hash, target_path):
        import tarfile
        
        assert type == 'git'

        # We don't want to require supplying a repo name, so for now
        # we simply iterate through all git repos
        try:
            repo_names = os.listdir(self.repo_path)
        except OSError, e:
            if e.errno != errno.ENOENT:
                raise
            repo_names = []
        for repo_name in repo_names:
            retcode, out, err = self.git(repo_name, 'rev-list', '-n1', '--quiet', hash)
            if retcode == 0:
                break
        else:
            raise KeyNotFoundError('Source item not present: git:%s' % hash)

        p = subprocess.Popen(['git', 'archive', '--format=tar', hash],
                             env=self.get_repo_env(repo_name),
                             stdout=subprocess.PIPE)
        # open in stream mode with the "r|" mode
        with closing(tarfile.open(fileobj=p.stdout, mode='r|')) as archive:
            archive.extractall(target_path)
        retcode = p.wait()
        if retcode != 0:
            raise CalledProcessError('git error: %d' % retcode)


SIMPLE_FILE_URL_RE = re.compile(r'^file:/?[^/]+.*$')

class ArchiveSourceCache(object):
    # Group together methods for working with the part of the source
    # cache stored as archives.

    chunk_size = 16 * 1024


    def __init__(self, source_cache):
        assert not isinstance(source_cache, str)
        self.source_cache = source_cache
        self.files_path = source_cache.cache_path
        self.packs_path = source_cache._ensure_subdir(PACKS_DIRNAME)
        self.mirrors = source_cache.mirrors
        self.logger = self.source_cache.logger

    def get_pack_filename(self, type, hash):
        d = self.files_path if type == 'files' else self.packs_path
        type_dir = pjoin(d, type)
        mkdir_if_not_exists(type_dir)
        return pjoin(type_dir, hash)

    def _download_and_hash(self, url, type):
        """Downloads file at url to a temporary location and hashes it

        Returns
        -------

        temp_file, digest
        """
        # Provide a special case for local files
        use_urllib = not SIMPLE_FILE_URL_RE.match(url)
        if not use_urllib:
            try:
                stream = open(url[len('file:'):])
            except IOError as e:
                raise SourceNotFoundError(str(e))
        else:
            # Make request.
            sys.stderr.write('Downloading %s...\n' % url)
            try:
                stream = urllib2.urlopen(url)
            except urllib2.HTTPError, e:
                raise RuntimeError("urllib failed to download (code: %d): %s" %\
                            (e.code, url))
            except urllib2.URLError, e:
                raise RuntimeError("urllib failed to download (reason: %s): %s" % (e.reason, url))

        # Download file to a temporary file within self.packs_path, while hashing
        # it.
        self.logger.info("Downloading '%s'" % url)
        temp_fd, temp_path = tempfile.mkstemp(prefix='downloading-', dir=self.packs_path)
        try:
            f = os.fdopen(temp_fd, 'wb')
            tee = HashingWriteStream(hashlib.sha256(), f)
            if use_urllib:
                progress = ProgressBar(int(stream.headers["Content-Length"]))
            try:
                n = 0
                while True:
                    chunk = stream.read(self.chunk_size)
                    if not chunk: break
                    if use_urllib:
                        n += len(chunk)
                        progress.update(n)
                    tee.write(chunk)
            finally:
                stream.close()
                f.close()
                if use_urllib:
                    progress.finish()
        except:
            # Remove temporary file if there was a failure
            os.unlink(temp_path)
            raise

        if not create_archive_handler(type).verify(temp_path):
            self.logger.error("File downloaded from '%s' is not a valid archive" % url)
            raise SourceNotFoundError("File downloaded from '%s' is not a valid archive" % url)

        return temp_path, format_digest(tee)

    def _ensure_type(self, url, type):
        if type is not None:
            if type not in archive_types:
                raise ValueError('Unknown archive type: %s' % type)
            return type
        else:
            for ext, it_type in archive_ext_to_type.items():
                if url.endswith('.' + ext):
                    return it_type
            raise ValueError('Unable to guess archive type of "%s"' % url)

    def contains(self, type, hash):
        return os.path.exists(self.get_pack_filename(type, hash))

    def fetch_from_mirrors(self, type, hash):
        for mirror in self.mirrors:
            url = '%s/%s/%s/%s' % (mirror, PACKS_DIRNAME, type, hash)
            try:
                self._download_archive(url, type, hash)
            except SourceNotFoundError:
                continue
            else:
                return True # found it
        return False

    def fetch(self, url, type, hash, repo_name):
        if type == 'files:':
            raise NotImplementedError("use the put() method to store raw files")
        else:
            self.fetch_archive(url, type, hash)

    def fetch_archive(self, url, type, expected_hash):
        if expected_hash is not None:
            found = self.contains(type, expected_hash)
            if not found:
                found = self.fetch_from_mirrors(type, expected_hash)
            if found:
                return '%s:%s' % (type, expected_hash)
        return self._download_archive(url, type, expected_hash)

    def _download_archive(self, url, type, expected_hash):
        type = self._ensure_type(url, type)
        temp_file, hash = self._download_and_hash(url, type)
        try:
            if expected_hash is not None and expected_hash != hash:
                raise RuntimeError('File downloaded from "%s" has hash %s but expected %s' %
                                   (url, hash, expected_hash))
            # Simply rename to the target; again a race shouldn't
            # matter with, in this case, identical content. Make it
            # read-only and readable for everybody, everybody can read
            os.chmod(temp_file, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.rename(temp_file, self.get_pack_filename(type, hash))
        finally:
            silent_unlink(temp_file)
        return '%s:%s' % (type, hash)

    def put(self, files):
        if isinstance(files, dict):
            files = files.items()
        key = hit_pack(files)
        type, hash = key.split(':')
        pack_filename = self.get_pack_filename(type, hash)
        if not os.path.exists(pack_filename):
            with file(pack_filename, 'w') as f:
                hit_pack(files, f)
        return key
    
    def unpack(self, type, hash, target_dir):
        infile = self.open_file(type, hash)
        with infile:
            if type == 'files':
                files = hit_unpack(infile, 'files:%s' % hash)
                scatter_files(files, target_dir)
            else:
                try:
                    create_archive_handler(type).unpack(infile, target_dir, hash)
                except SourceCacheError, e:
                    self.logger.error(str(e))
                    raise

    def open_file(self, type, hash):
        try:
            f = file(self.get_pack_filename(type, hash))
        except IOError, e:
            if e.errno == errno.ENOENT:
                raise KeyNotFoundError("%s:%s" % (type, hash))
        return f

    #
    # hit packs
    #
    def _extract_hit_pack(self, f, key, target_dir):
        files = hit_unpack(f, key)
        scatter_files(files, target_dir)        


#
# Archive format support
#

def common_path_prefix(paths):
    if len(paths) == 0:
        return 0
    sep = os.path.sep
    prefixes = [p.split(sep)[:-1] for p in paths]
    common_prefix = paths[0].split(sep)[:-1]
    for path in paths[1:]:
        prefix = path.split(sep)[:-1]
        if len(prefix) < common_prefix:
            common_prefix = common_prefix[:len(prefix)]
        for i, (this_part, common_part) in enumerate(zip(prefix, common_prefix)):
            if this_part != common_part:
                common_prefix = common_prefix[:i]
                break
    if len(common_prefix) == 0:
        return ''
    else:
        return sep.join(common_prefix) + sep

class TarballHandler(object):
    chunk_size = 16 * 1024
    
    def verify(self, filename):
        import tarfile
        try:
            with closing(tarfile.open(filename, self.read_mode)) as archive:
                # Just in case, make sure we can actually read the archive:
                members = archive.getmembers()
            return True
        except tarfile.ReadError:
            return False

    def unpack(self, infile, target_dir, hash):
        import tarfile
        from StringIO import StringIO
        target_dir = os.path.abspath(target_dir)

        archive_data = infile.read()
        if format_digest(hashlib.sha256(archive_data)) != hash:
            raise CorruptSourceCacheError("Corrupted file: '%s'" % infile.name)

        with closing(tarfile.open(fileobj=StringIO(archive_data), mode=self.read_mode)) as archive:
            members = archive.getmembers()
            prefix_len = len(common_path_prefix([member.name for member in members
                                                 if member.type != tarfile.DIRTYPE]))
            # Filter away too short directory entries, remove prefix,
            # and prevent directory escape attacks
            filtered_members = []
            for member in members:
                if len(member.name) <= prefix_len:
                    continue
                if not os.path.abspath(pjoin(target_dir, member.name)).startswith(target_dir):
                    raise SecurityError("Archive attempted to break out of target dir "
                                        "with filename: %s" % member.name)
                member.name = member.name[prefix_len:]
            archive.extractall(target_dir, members)


class TarGzHandler(TarballHandler):
    type = 'tar.gz'
    exts = ['tar.gz', 'tgz']
    read_mode = 'r:gz'


class TarBz2Handler(TarballHandler):
    type = 'tar.bz2'
    exts = ['tar.bz2', 'tb2', 'tbz2']
    read_mode = 'r:bz2'

class ZipHandler(object):
    type = 'zip'
    exts = ['zip']
    def verify(self, filename):
        from zipfile import ZipFile
        with closing(ZipFile(filename)) as f:
            return f.testzip() is None # returns None if zip is OK

    def unpack(self, infile, target_dir, hash):
        from zipfile import ZipFile
        from StringIO import StringIO
        # only safe mode implemented as ZipFile does random access
        archive_data = infile.read()
        if format_digest(hashlib.sha256(archive_data)) != hash:
            raise CorruptSourceCacheError("Corrupted file: '%s'" % infile.name)
        with closing(ZipFile(StringIO(archive_data))) as f:
            infolist = f.infolist()
            if len(infolist) == 0:
                return
            # Scan through infolist to determine length of common prefix, and modify ZipInfo structs
            # during extraction
            prefix_len = len(common_path_prefix([info.filename for info in infolist]))
            for info in infolist:
                info.filename = info.filename[prefix_len:]
                f.extract(info, target_dir)
        
archive_ext_to_type = {}
archive_handler_classes = {}
archive_types = []
for cls in [TarGzHandler, TarBz2Handler, ZipHandler]:
    for ext in cls.exts:
        archive_ext_to_type[ext] = cls.type
    archive_handler_classes[cls.type] = cls
    archive_types.append(cls.type)
archive_types = sorted(archive_types)

        
def create_archive_handler(type):
    return archive_handler_classes[type]()

def hit_pack(files, stream=None):
    """
    Packs the given files in the "hit-pack" format documented above,
    and returns the resulting key. This is
    useful to hash a set of files solely by their contents, not
    metadata, except the filename.

    Parameters
    ----------

    files : list of (filename, contents)
        The contents of the archive. `filename` may contain forward
        slashes ``/`` as path separators. `contents` is a pure bytes
        objects which will be dumped directly to `stream`.

    stream : file-like (optional)
        Result of the packing, or `None` if one only wishes to know
        the hash.

    Returns
    -------

    The key of the resulting pack
    (e.g., ``files:cmRX4RyxU63D9Ciq8ZAfxWGjdMMOXn2mdCwHQqM4Zjw``).
    """
    tee = HashingWriteStream(hashlib.sha256(), stream)
    tee.write('HDSTPCK1')
    files = sorted(files)
    for filename, contents in files:
        tee.write(struct.pack('<II', len(filename), len(contents)))
        tee.write(filename)
        tee.write(contents)
    return 'files:%s' % format_digest(tee)

def hit_unpack(stream, key):
    """
    Unpacks the files in the "hit-pack" format documented above,
    verifies that it matches the given key, and returns the contents
    (in memory).

    Parameters
    ----------

    stream : file-like

        Stream to read the pack from

    key : str

        Result from :func:`hit_pack`.

    Returns
    -------

    list of (filename, contents)
    """
    if not key.startswith('files:'):
        raise ValueError('invalid key')
    digest = key[len('files:'):]
    tee = HashingReadStream(hashlib.sha256(), stream)
    if tee.read(8) != 'HDSTPCK1':
        raise CorruptSourceCacheError('Not an hit-pack')
    files = []
    while True:
        buf = tee.read(8)
        if not buf:
            break
        filename_len, contents_len = struct.unpack('<II', buf)
        filename = tee.read(filename_len)
        contents = tee.read(contents_len)
        files.append((filename, contents))
    if digest != format_digest(tee):
        raise CorruptSourceCacheError('hit-pack does not match key "%s"' % key)
    return files
        
def scatter_files(files, target_dir):
    """
    Given a list of filenames and their contents, write them to the file system.

    Will not overwrite files (raises an OSError(errno.EEXIST)).

    This is typically used together with :func:`hit_unpack`.

    Parameters
    ----------

    files : list of (filename, contents)

    target_dir : str
        Filesystem location to emit the files to
    """
    existing_dir_cache = set()
    existing_dir_cache.add(target_dir)
    for filename, contents in files:
        dirname, basename = os.path.split(filename)
        dirname = pjoin(target_dir, dirname)
        if dirname not in existing_dir_cache and not os.path.exists(dirname):
            os.makedirs(dirname)
            existing_dir_cache.add(dirname)

        # IIUC in Python 3.3+ one can do this with the 'x' file mode, but need to do it
        # ourselves currently
        fd = os.open(pjoin(dirname, basename), os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0600)
        with os.fdopen(fd, 'w') as f:
            f.write(contents)

def silent_unlink(path):
    try:
        os.unlink(temp_file)
    except:
        pass

