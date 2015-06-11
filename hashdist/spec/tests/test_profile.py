import mock
from pprint import pprint
import os
import shutil
import tempfile
import subprocess
import logging
from os.path import join as pjoin
from nose.tools import eq_, ok_

from ...formats.marked_yaml import yaml_dump
from ...core import SourceCache
from ...core.test.utils import *
from ...core.test.test_source_cache import temp_source_cache
from .. import profile
from .. import package
from .. import builder
from ..exceptions import ProfileError
from ..exceptions import PackageError


null_logger = logging.getLogger('null_logger')


def gitify(dir):
    with working_directory(dir):
        subprocess.check_call(['git', 'init'], stdout=open(os.devnull, 'wb'))
        subprocess.check_call(['git', 'add', '.'], stdout=open(os.devnull, 'wb'))
        subprocess.check_call(['git', 'commit', '-m', 'Initial commit'], stdout=open(os.devnull, 'wb'))
        p = subprocess.Popen(['git', 'rev-parse', 'HEAD'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        commit = out.strip()
        assert p.wait() == 0
    return commit

def setup():
    """
    We set up a test setup with three directories, 'user', 'base1' and 'base2'.
    'base2' is made into a git repository which is checked out.

    """
    global tmpdir, source_cache
    tmpdir = tempfile.mkdtemp()
    source_cache = SourceCache(tmpdir, logger)

def teardown():
    shutil.rmtree(tmpdir)


@temp_working_dir_fixture
def test_temp_git_checkouts(d):
    os.mkdir(pjoin(d, 'src'))
    repo1_dir = pjoin(d, 'repo1')
    repo2_dir = pjoin(d, 'repo2')
    dump(pjoin(repo1_dir, 'README1'), 'Hello 1')
    dump(pjoin(repo2_dir, 'README2'), 'Hello 2')
    repo1_commit = 'git:' + gitify(repo1_dir)
    repo2_commit = 'git:' + gitify(repo2_dir)
    sc = SourceCache(pjoin(d, 'src'), logger)
    with profile.TemporarySourceCheckouts(sc) as chk:
        tmp1 = chk.checkout('repo1', repo1_commit, [repo1_dir])
        # Idempotency
        assert tmp1 == chk.checkout('repo1', repo1_commit, [repo1_dir])
        # Using same name for more than one commit is illegal
        with assert_raises(ProfileError):
            tmp2 = chk.checkout('repo1', repo2_commit, [repo1_dir])
        tmp2 = chk.checkout('repo2', repo2_commit, [repo2_dir])
        assert os.path.exists(pjoin(tmp1, 'README1'))
        assert os.path.exists(pjoin(tmp2, 'README2'))

        assert chk.resolve('<repo1>/foo/bar') == tmp1 + '/foo/bar'
        assert chk.resolve('<repo2>/foo/bar') == tmp2 + '/foo/bar'
        assert chk.resolve('/<repo2>/bar') == '/<repo2>/bar'
        with assert_raises(ProfileError):
            chk.resolve('<no_such_repo>/bar')

    assert not os.path.exists(tmp1)
    assert not os.path.exists(tmp2)


@temp_working_dir_fixture
def test_load_and_inherit_profile_dir_treatment(d):
    # Test resolution over git and how package_dirs and hook_import_dirs responds
    dump(pjoin(d, 'gitrepo', 'in_parent_dir.yaml'), """\
        package_dirs:
        - pkgs_parentdir
        hook_import_dirs:
        - utils
        - /some/absolute/path
    """)
    dump(pjoin(d, 'gitrepo', 'subdir', 'in_sub_dir.yaml'), """\
        package_dirs:
        - pkgs_subdir
        hook_import_dirs:
        - utils
        extends:
          - file: ../in_parent_dir.yaml
    """)
    commit = gitify(pjoin(d, 'gitrepo'))
    dump(pjoin(d, 'user', 'profile.yaml'), """\
        package_dirs:
        - pkgs_user
        hook_import_dirs:
        - utils
        extends:
          - file: subdir/in_sub_dir.yaml
            urls: [%s]
            key: git:%s
            name: repo1
    """ % (pjoin(d, 'gitrepo'), commit))
    os.mkdir(pjoin(d, 'src'))
    with profile.TemporarySourceCheckouts(SourceCache(pjoin(d, 'src'), logger)) as checkouts:
        doc = profile.load_and_inherit_profile(checkouts, pjoin(d, 'user', 'profile.yaml'))
        assert doc['hook_import_dirs'] == ['%s/user/utils' % d, '<repo1>/subdir/utils',
                                           '<repo1>/subdir/../utils', '/some/absolute/path']
        assert doc['package_dirs'] == ['%s/user/pkgs_user' % d, '<repo1>/subdir/pkgs_subdir',
                                       '<repo1>/subdir/../pkgs_parentdir']

@temp_working_dir_fixture
def test_profile_parameters(d):
    dump(pjoin(d, "profile.yaml"), """\
        extends:
        - file: base1.yaml
        - file: base2.yaml
        parameters:
          a: 1
          b: 2
          set_in_both_but_overridden: 0
    """)

    dump("base1.yaml", """\
        parameters:
          a: 0
          c: 3
          set_in_both_but_overridden: 4
    """)

    dump("base2.yaml", """\
        parameters:
          d: 4
          set_in_both_but_overridden: 5
    """)

    with profile.TemporarySourceCheckouts(None) as checkouts:
        doc = profile.load_and_inherit_profile(checkouts, "profile.yaml")
    assert doc['parameters'] == {'a': 1, 'b': 2, 'c': 3, 'd': 4,
                                 'set_in_both_but_overridden': 0}

@temp_working_dir_fixture
def test_parameter_collision(d):
    dump(pjoin(d, "profile.yaml"), """\
        extends:
        - file: base1.yaml
        - file: base2.yaml
    """)

    dump("base1.yaml", "parameters: {a: 0}")
    dump("base2.yaml", "parameters: {a: 1}")
    with profile.TemporarySourceCheckouts(None) as checkouts:
        with assert_raises(ProfileError):
            doc = profile.load_and_inherit_profile(checkouts, "profile.yaml")

@temp_working_dir_fixture
def test_file_resolver(d):
    dump(pjoin(d, "level2", "pkgs", "foo", "foo.yaml"), "{my: document}")
    dump(pjoin(d, "level1", "pkgs", "foo.yaml"), "1")
    dump(pjoin(d, "level1", "pkgs", "bar.yaml"), "1")
    with profile.TemporarySourceCheckouts(None) as checkouts:
        r = profile.FileResolver(checkouts, [pjoin(d, 'level2'), pjoin(d, 'level1')])
        assert pjoin(d, "level2", "pkgs", "foo", "foo.yaml") == r.find_file([
            "pkgs/foo.yaml", "pkgs/foo/foo.yaml"
            ])
        assert pjoin(d, "level1", "pkgs", "bar.yaml") == r.find_file([
            "pkgs/bar.yaml", "pkgs/bar/bar.yaml"
            ])

@temp_working_dir_fixture
def test_file_resolver_glob(d):
    class MockCheckoutsManager(object):
        def resolve(self, x): return x

    dump(pjoin(d, "level2", "bar.yaml"), "{my: document}")
    dump(pjoin(d, "level2", "foo", "foo-0.yaml"), "{my: document}") # matched twice, returned once
    dump(pjoin(d, "level2", "foo", "foo-1.yaml"), "{my: document}") # overrides level1
    dump(pjoin(d, "level1", "foo", "foo-0.yaml"), "{my: document}")
    dump(pjoin(d, "level1", "foo", "foo-1.yaml"), "{my: document}") # overriden by level2
    dump(pjoin(d, "level1", "foo", "foo-2.yaml"), "{my: document}")
    dump(pjoin(d, "level1", "foo", "foo-3.yaml"), "{my: document}")
    r = profile.FileResolver(MockCheckoutsManager(), [pjoin(d, 'level2'), pjoin(d, 'level1')])
    matches = r.glob_files(['foo/foo-*.yaml', 'foo/*0.yaml', 'bar.yaml'])
    eq_(matches, {
        'bar.yaml': ('bar.yaml', '%s/level2/bar.yaml' % d),
        'foo/foo-0.yaml': ('foo/*0.yaml', '%s/level2/foo/foo-0.yaml' % d),
        'foo/foo-1.yaml': ('foo/foo-*.yaml', '%s/level2/foo/foo-1.yaml' % d),
        'foo/foo-2.yaml': ('foo/foo-*.yaml', '%s/level1/foo/foo-2.yaml' % d),
        'foo/foo-3.yaml': ('foo/foo-*.yaml', '%s/level1/foo/foo-3.yaml' % d)})


@temp_working_dir_fixture
def test_resource_resolution(d):
    # test packages_dir, base_dir, and sys.path
    dump(pjoin(d, "level3", "profile.yaml"), """\
        extends:
          - file: %s/level2/profiles/profile.yaml
    """ % d)

    dump(pjoin(d, "level2", "profiles", "profile.yaml"), """\
        extends:
          - file: %s/level1/profile.yaml
        package_dirs: [../pkgs, ../base]
    """ % d)

    dump(pjoin(d, "level1","profile.yaml"), """\
        package_dirs: [pkgs, base]
    """)

    dump(pjoin(d, "level2", "base", "base.yaml"), "{my: base}")
    dump(pjoin(d, "level1", "base", "base.yaml"), "{}")
    dump(pjoin(d, "level1", "base", "base1.txt"), "{}")
    dump(pjoin(d, "level2", "pkgs", "foo", "foo.yaml"), "{my: document}")
    dump(pjoin(d, "level1", "pkgs", "foo.yaml"), "{}")
    dump(pjoin(d, "level1", "pkgs", "bar.yaml"), "{}")

    with profile.TemporarySourceCheckouts(None) as checkouts:
        doc = profile.load_and_inherit_profile(checkouts, pjoin(d, "level3", "profile.yaml"))
        p = profile.Profile(null_logger, doc, checkouts)
        assert (pjoin(d, "level2", "pkgs", "foo", "foo.yaml") ==
                os.path.realpath(p.find_package_file("foo", "foo.yaml")))
        assert (pjoin(d, "level1", "pkgs", "bar.yaml") ==
                os.path.realpath(p.find_package_file("bar", "bar.yaml")))
        assert (pjoin(d, "level2", "base", "base.yaml") ==
                os.path.realpath(p.find_package_file("whatever", "base.yaml")))
        assert (pjoin(d, "level1", "base", "base1.txt") ==
                os.path.realpath(p.find_package_file("whatever", "base1.txt")))

        os.unlink(pjoin(d, "level2", "pkgs", "foo", "foo.yaml"))
        assert pjoin(d, "level1", "pkgs", "foo.yaml") == p.find_package_file("foo", "foo.yaml")


@temp_working_dir_fixture
def test_load_and_inherit_profile(d):
    dump(pjoin(d, "user.yaml"), """\
        extends:
          - file: base1.yaml
          - file: base2.yaml

        packages:
          gcc:
          numpy:
            host: false
          to-be-deleted:
            skip: true

    """)

    dump(pjoin(d, "base1.yaml"), """\
        packages:
          to-be-deleted: # skipped in user/profile.yaml
          mpi:
            use: openmpi
    """)

    dump(pjoin(d, "base2.yaml"), """\
        packages:
          numpy:
            host: true # changed in user/profile.yaml
          python:
            host: true # not changed
    """)


    with profile.TemporarySourceCheckouts(None) as checkouts:
        p = profile.load_and_inherit_profile(checkouts, pjoin(d, "user.yaml"))
    eq_(p['packages'], {'python': {'host': True},
                        'numpy': {'host': False},
                        'gcc': {},
                        'mpi': {'use': 'openmpi'}})


@temp_working_dir_fixture
def test_defaults_section_in_package(d):
    dump(pjoin(d, "without_override.yaml"), """\
        package_dirs:
        - .
        packages:
          mypkg:
    """)

    dump(pjoin(d, "with_package.yaml"), """\
        package_dirs:
        - .
        packages:
          mypkg:
            foo: false
            exit_code: 1
    """)

    dump(pjoin(d, "with_global.yaml"), """\
        package_dirs:
        - .
        parameters:
          foo: false
          exit_code: 1
        packages:
          mypkg:
    """)

    dump(pjoin(d, "mypkg.yaml"), """\
        defaults:
          foo: true
          exit_code: 0
        build_stages:
          - handler: bash
            bash: |
              exit {{exit_code}}

          - when: foo
            handler: bash
    """)
    def get_build_stages_of_mypkg(profile_file):
        with profile.TemporarySourceCheckouts(None) as checkouts:
            doc = profile.load_and_inherit_profile(checkouts, pjoin(d, profile_file))
            prf = profile.Profile(null_logger, doc, checkouts)

            # Test parameters present structurally
            pkg = prf.load_package('mypkg')
            assert 'foo' in pkg.parameters
            assert 'exit_code' in pkg.parameters
            assert pkg.parameters['foo'].default == True

            # Use builder to resulve parameters
            mock_sc = mock.Mock()
            mock_sc.put = lambda files: 'files:thekey'
            mock_bs = mock.Mock()
            mock_bs.is_present = lambda *args: True
            pb = builder.ProfileBuilder(null_logger, mock_sc, mock_bs, prf)
            pb._load_packages()
            pkginst = pb._packages['mypkg']
            return pkginst._impl.doc['build_stages']


    assert get_build_stages_of_mypkg('without_override.yaml') == [
        {'handler': 'bash'}, {'handler': 'bash', 'bash': 'exit 0\n'}]
    assert get_build_stages_of_mypkg('with_global.yaml') == [{'handler': 'bash', 'bash': 'exit 1\n'}]
    assert get_build_stages_of_mypkg('with_package.yaml') == [{'handler': 'bash', 'bash': 'exit 1\n'}]


def std_profile(d):
    dump(pjoin(d, "profile.yaml"), """\
        package_dirs: [.]
        packages:
          foo:
    """)


def build_profile(d):
    with profile.TemporarySourceCheckouts(None) as checkouts:
        doc = profile.load_and_inherit_profile(checkouts, pjoin(d, 'profile.yaml'))
        prof = profile.Profile(null_logger, doc, checkouts)
    return prof


@temp_working_dir_fixture
def test_not_providing_required_parameter(d):
    std_profile(d)
    dump(pjoin(d, "foo.yaml"), """\
        parameters: [{name: needed}]
    """)

    prof = build_profile(d)
    with assert_raises(ProfileError) as e:
        prof.resolve_parameters()
    assert 'constraint not satisfied: "needed is not None"' in str(e.exc_val)


@temp_working_dir_fixture
def test_not_declaring_parameter(d):
    std_profile(d)

    dump(pjoin(d, "foo.yaml"), """\
        build_stages:
        - when: foo
          handler: bash
    """)

    prof = build_profile(d)
    with assert_raises(ProfileError) as e:
        prof.resolve_parameters()
    assert "parameter not defined: name 'foo' is not defined" in str(e.exc_val)


@temp_working_dir_fixture
def test_constraints(d):
    dump(pjoin(d, "profile.yaml"), """\
        package_dirs: [.]
        packages:
          foo:
            x: bar
            y: whatever
    """)

    dump(pjoin(d, "foo.yaml"), """\
        parameters:
        - name: x
        - name: y

        constraints:
        - when y == 'check_that_x_is_not_foo':
            - x != 'foo'
        - x != 'bar'
    """)

    prof = build_profile(d)
    with assert_raises(ProfileError) as e:
        prof.resolve_parameters()
    assert "constraint not satisfied: \"x != 'bar'\"" in str(e.exc_val)

    dump(pjoin(d, "profile.yaml"), """\
        package_dirs: [.]
        packages:
          foo:
            x: foo
            y: check_that_x_is_not_foo
    """)

    prof = build_profile(d)
    with assert_raises(ProfileError) as e:
        prof.resolve_parameters()
    assert "constraint not satisfied: \"not (y == 'check_that_x_is_not_foo') or (x != 'foo')\"" in str(e.exc_val)

    dump(pjoin(d, "profile.yaml"), """\
        package_dirs: [.]
        packages:
          foo:
            x: legal
            y: legal
    """)
    build_profile(d).resolve_parameters()


@temp_working_dir_fixture
def test_version_constraints(d):
    dump(pjoin(d, "profile.yaml"), """\
        package_dirs: [.]
        packages:
          foo:
          bar:
            version: 1.2b0
    """)

    dump(pjoin(d, "foo.yaml"), """\
        parameters:
        - name: version
          type: version
          default: 10.0
        dependencies:
          build: [bar]
        constraints:
        - 0.1 <= bar.version < 1.3
    """)
    dump(pjoin(d, "bar.yaml"), """\
        parameters:
        - name: version
          type: version
    """)
    prof = build_profile(d)
    prof.resolve_parameters()


    dump(pjoin(d, "profile.yaml"), """\
        package_dirs: [.]
        packages:
          foo:
          bar: {version: 10.0}
        """)
    prof = build_profile(d)
    with assert_raises(ProfileError) as e:
        prof.resolve_parameters()
    assert 'constraint not satisfied: "0.1 <= bar.version < 1.3"' in str(e.exc_val)


@temp_working_dir_fixture
def test_conditionals_in_profile(d):
    # note: {{expr}}-expansion not supported yet as we're not parsing expressions
    # so don't know their dependencies
    dump(pjoin(d, "base_profile.yaml"), """\
    package_dirs: [.]
    parameters:
      y:
        when platform == 'linux': 'onlinux'
        when platform == 'windows': 'onwindows'

    packages:
      foo:
        x1:
          when platform == 'linux': '1'
          when platform == 'windows': '2'
        x2:  # overridden in sub-profile
          when platform == 'linux': '1'
          when platform == 'windows': '2'
    """)

    dump(pjoin(d, 'profile.yaml'), """\
    extends:
    - file: base_profile.yaml
    parameters:
      platform: linux
      x2:  # overridden in packages.foo
        when y == 'onlinux': '{{y}}_chosen'
        when y == 'onwindows': '{{y}}_chosen'
      x3:  # not overridden
        when y == 'onlinux': '{{y}}_chosen'
        when y == 'onwindows': '{{y}}_chosen'
    packages:
      foo:
        x2: '3'
      bar:
        # Change the platform variable for this package, and everything else
        # changes too..
        platform: 'windows'
    """)

    dump(pjoin(d, 'foo.yaml'), """\
    parameters:
      - name: x1
      - name: x2
      - name: y
    """)
    dump(pjoin(d, 'bar.yaml'), """\
    parameters:
      - name: x2
      - name: y
    """)
    prof = build_profile(d)
    p = prof.apply_parameter_rules(prof.packages['foo'])
    eq_(p, {'x2': u'3', 'platform': u'linux', 'y': u'onlinux', 'x3': u'onlinux_chosen', 'x1': '1'})
    p = prof.apply_parameter_rules(prof.packages['bar'])
    eq_(p, {'y': u'onwindows', 'platform': u'windows', 'x2': u'onwindows_chosen', 'x3': u'onwindows_chosen'})

    pkgs = prof.resolve_parameters()
    eq_(pkgs['foo'].x2, '3')
    eq_(pkgs['bar'].x2, 'onwindows_chosen')
