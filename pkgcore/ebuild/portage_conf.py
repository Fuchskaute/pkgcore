# Copyright: 2006-2011 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

"""make.conf translator.

Converts portage configuration files into :obj:`pkgcore.config` form.
"""

__all__ = (
    "SecurityUpgradesViaProfile", "make_repo_syncers",
    "add_sets", "add_profile", "add_fetcher", "make_cache",
    "config_from_make_conf",
)

import os

from snakeoil.compatibility import raise_from, IGNORED_EXCEPTIONS
from snakeoil.demandload import demandload
from snakeoil.mappings import ImmutableDict
from snakeoil.osutils import access, normpath, abspath, listdir_files, pjoin, ensure_dirs

from pkgcore import const
from pkgcore.config import basics, configurable
from pkgcore.ebuild import const as econst
from pkgcore.ebuild.repo_objs import RepoConfig
from pkgcore.pkgsets.glsa import SecurityUpgrades

demandload(
    'errno',
    'snakeoil.bash:read_bash_dict',
    'snakeoil.compatibility:ConfigParser',
    'snakeoil.xml:etree',
    'pkgcore.config:errors',
    'pkgcore.ebuild:profiles',
    'pkgcore.fs.livefs:iter_scan',
    'pkgcore.log:logger',
)


def my_convert_hybrid(manager, val, arg_type):
    """Modified convert_hybrid using a sequence of strings for section_refs."""
    if arg_type.startswith('refs:'):
        subtype = 'ref:' + arg_type.split(':', 1)[1]
        return [basics.LazyNamedSectionRef(manager, subtype, name) for name in val]
    return basics.convert_hybrid(manager, val, arg_type)


@configurable({'ebuild_repo': 'ref:repo', 'vdb': 'ref:repo',
               'profile': 'ref:profile'}, typename='pkgset')
def SecurityUpgradesViaProfile(ebuild_repo, vdb, profile):
    """
    generate a GLSA vuln. pkgset limited by profile

    :param ebuild_repo: :obj:`pkgcore.ebuild.repository.UnconfiguredTree` instance
    :param vdb: :obj:`pkgcore.repository.prototype.tree` instance that is the livefs
    :param profile: :obj:`pkgcore.ebuild.profiles` instance
    """
    arch = profile.arch
    if arch is None:
        raise errors.ComplexInstantiationError("arch wasn't set in profiles")
    return SecurityUpgrades(ebuild_repo, vdb, arch)


def isolate_rsync_opts(options):
    """
    pop the misc RSYNC related options littered in make.conf, returning
    a base rsync dict
    """
    base = {}
    opts = []
    extra_opts = []

    opts.extend(options.pop('PORTAGE_RSYNC_OPTS', '').split())
    extra_opts.extend(options.pop('PORTAGE_RSYNC_EXTRA_OPTS', '').split())

    timeout = options.pop('PORTAGE_RSYNC_INITIAL_TIMEOUT', None)
    if timeout is not None:
        base['connection_timeout'] = timeout

    retries = options.pop('PORTAGE_RSYNC_RETRIES', None)
    if retries is not None:
        try:
            retries = int(retries)
            if retries < 0:
                retries = 10000
            base['retries'] = str(retries)
        except ValueError:
            pass

    proxy = options.pop('RSYNC_PROXY', None)
    if proxy is not None:
        base['proxy'] = proxy.strip()

    if opts:
        base['opts'] = tuple(opts)
    if extra_opts:
        base['extra_opts'] = tuple(extra_opts)

    return base


def make_repo_syncers(config, repos_conf, make_conf, allow_timestamps=True):
    """generate syncing configs for known repos"""
    rsync_opts = None

    for repo_opts in repos_conf.itervalues():
        d = {'basedir': repo_opts['location']}

        sync_type = repo_opts.get('sync-type', None)
        sync_uri = repo_opts.get('sync-uri', None)

        if sync_uri:
            # prefix non-native protocols
            if (sync_type is not None and not sync_uri.startswith(sync_type)):
                sync_uri = '%s+%s' % (sync_type, sync_uri)

            d['uri'] = sync_uri

            if sync_type == 'rsync':
                if rsync_opts is None:
                    # various make.conf options used by rsync-based syncers
                    rsync_opts = isolate_rsync_opts(make_conf)
                d.update(rsync_opts)
                if allow_timestamps:
                    d['class'] = 'pkgcore.sync.rsync.rsync_timestamp_syncer'
                else:
                    d['class'] = 'pkgcore.sync.rsync.rsync_syncer'
            else:
                d['class'] = 'pkgcore.sync.base.GenericSyncer'
        elif sync_uri is None:
            # try to autodetect syncing mechanism if sync-uri is missing
            d['class'] = 'pkgcore.sync.base.AutodetectSyncer'
        else:
            # disable syncing if sync-uri is explicitly unset
            d['class'] = 'pkgcore.sync.base.DisabledSyncer'

        name = 'sync:%s' % repo_opts['location']
        config[name] = basics.AutoConfigSection(d)


def add_sets(config, root, portage_base_dir):
    config["world"] = basics.AutoConfigSection({
        "class": "pkgcore.pkgsets.filelist.WorldFile",
        "location": pjoin(root, econst.WORLD_FILE)})
    config["system"] = basics.AutoConfigSection({
        "class": "pkgcore.pkgsets.system.SystemSet",
        "profile": "profile"})
    config["installed"] = basics.AutoConfigSection({
        "class": "pkgcore.pkgsets.installed.Installed",
        "vdb": "vdb"})
    config["versioned-installed"] = basics.AutoConfigSection({
        "class": "pkgcore.pkgsets.installed.VersionedInstalled",
        "vdb": "vdb"})

    set_fp = pjoin(portage_base_dir, "sets")
    try:
        for setname in listdir_files(set_fp):
            # Potential for name clashes here, those will just make
            # the set not show up in config.
            if setname in ("system", "world"):
                logger.warning(
                    "user defined set %s is disallowed; ignoring" %
                    pjoin(set_fp, setname))
                continue
            config[setname] = basics.AutoConfigSection({
                "class": "pkgcore.pkgsets.filelist.FileList",
                "location": pjoin(set_fp, setname)})
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

def _find_profile_link(base_path, portage_compat=False):
    make_profile = pjoin(base_path, 'make.profile')
    try:
        return normpath(abspath(
            pjoin(base_path, os.readlink(make_profile))))
    except EnvironmentError as oe:
        if oe.errno in (errno.ENOENT, errno.EINVAL):
            if oe.errno == errno.ENOENT:
                if portage_compat:
                    return None
                profile = _find_profile_link(pjoin(base_path, 'portage'), True)
                if profile is not None:
                    return profile
            raise_from(errors.ComplexInstantiationError(
                "%s must be a symlink pointing to a real target" % (
                    make_profile,)))
        raise_from(errors.ComplexInstantiationError(
            "%s: unexpected error- %s" % (make_profile, oe.strerror)))

def add_profile(config, base_path, user_profile_path=None, profile_override=None):
    if profile_override is None:
        profile = _find_profile_link(base_path)
    else:
        profile = normpath(abspath(profile_override))
        if not os.path.exists(profile):
            raise_from(errors.ComplexInstantiationError(
                "%s doesn't exist" % (profile,)))

    paths = profiles.OnDiskProfile.split_abspath(profile)
    if paths is None:
        raise errors.ComplexInstantiationError(
            '%s expands to %s, but no profile detected' %
            (pjoin(base_path, 'make.profile'), profile))

    if os.path.isdir(user_profile_path):
        config["profile"] = basics.AutoConfigSection({
            "class": "pkgcore.ebuild.profiles.UserProfile",
            "parent_path": paths[0],
            "parent_profile": paths[1],
            "user_path": user_profile_path,
        })
    else:
        config["profile"] = basics.AutoConfigSection({
            "class": "pkgcore.ebuild.profiles.OnDiskProfile",
            "basepath": paths[0],
            "profile": paths[1],
        })


def add_fetcher(config, make_conf, distdir):
    fetchcommand = make_conf.pop("FETCHCOMMAND")
    resumecommand = make_conf.pop("RESUMECOMMAND", fetchcommand)

    # copy it to prevent modification.
    # map a config arg to an obj arg, pop a few values
    fetcher_dict = dict(make_conf)
    if "FETCH_ATTEMPTS" in fetcher_dict:
        fetcher_dict["attempts"] = fetcher_dict.pop("FETCH_ATTEMPTS")
    fetcher_dict.pop("readonly", None)
    fetcher_dict.update({
        "class": "pkgcore.fetch.custom.fetcher",
        "distdir": distdir,
        "command": fetchcommand,
        "resume_command": resumecommand,
    })
    config["fetcher"] = basics.AutoConfigSection(fetcher_dict)


def make_cache(config_root, repo_path):
    # TODO: probably should pull RepoConfig objects dynamically from the config
    # instead of regenerating them
    repo_config = RepoConfig(repo_path)

    # Use md5 cache if it exists or the option is selected, otherwise default to
    # the old flat hash format in /var/cache/edb/dep/*.
    if (os.path.exists(pjoin(repo_path, 'metadata', 'md5-cache')) or
            repo_config.cache_format == 'md5-dict'):
        kls = 'pkgcore.cache.flat_hash.md5_cache'
        repo_path = pjoin(config_root, repo_path.lstrip('/'))
        cache_parent_dir = pjoin(repo_path, 'metadata', 'md5-cache')
    else:
        kls = 'pkgcore.cache.flat_hash.database'
        repo_path = pjoin(config_root, 'var', 'cache', 'edb', 'dep', repo_path.lstrip('/'))
        cache_parent_dir = repo_path

    while not os.path.exists(cache_parent_dir):
        cache_parent_dir = os.path.dirname(cache_parent_dir)
    readonly = (not access(cache_parent_dir, os.W_OK | os.X_OK))

    return basics.AutoConfigSection({
        'class': kls,
        'location': repo_path,
        'readonly': readonly
    })


def load_make_conf(vars_dict, path, allow_sourcing=False, required=True,
                     incrementals=False):
    sourcing_command = None
    if allow_sourcing:
        sourcing_command = 'source'
    try:
        new_vars = read_bash_dict(
            path, vars_dict=vars_dict, sourcing_command=sourcing_command)
    except EnvironmentError as e:
        if e.errno == errno.EACCES:
            raise_from(errors.PermissionDeniedError(path, write=False))
        if e.errno != errno.ENOENT or required:
            raise_from(errors.ParsingError("parsing %r" % (path,), exception=e))
        return

    if incrementals:
        for key in econst.incrementals:
            if key in vars_dict and key in new_vars:
                new_vars[key] = "%s %s" % (vars_dict[key], new_vars[key])
    # quirk of read_bash_dict; it returns only what was mutated.
    vars_dict.update(new_vars)


def load_repos_conf(path):
    """parse repos.conf files

    :param path: path to the repos.conf which can be a regular file or
        directory, if a directory is passed all the non-hidden files within
        that directory are parsed in alphabetical order.
    """
    if os.path.isdir(path):
        files = iter_scan(path)
        files = sorted(x.location for x in files if x.is_reg
                       and not x.basename.startswith('.'))
    else:
        files = [path]

    defaults = {}
    repos = {}
    for fp in files:
        try:
            with open(fp) as f:
                config = ConfigParser()
                config.read_file(f)
        except EnvironmentError as e:
            if e.errno == errno.EACCES:
                raise_from(errors.PermissionDeniedError(fp, write=False))
            raise_from(errors.ParsingError("parsing %r" % (fp,), exception=e))

        defaults.update(config.defaults())
        for repo_name in config.sections():
            repos[repo_name] = {k: v for k, v in config.items(repo_name)}

            # repo priority defaults to zero if unset
            priority = repos[repo_name].get('priority', 0)
            try:
                repos[repo_name]['priority'] = int(priority)
            except ValueError:
                raise errors.ParsingError(
                    "%s: repo '%s' has invalid priority setting: %s" %
                    (fp, repo_name, priority))

            # only the location setting is strictly required
            if 'location' not in repos[repo_name]:
                raise errors.ParsingError(
                    "%s: repo '%s' missing location setting" %
                    (fp, repo_name))

    # the default repo is gentoo if unset
    default_repo = defaults.get('main-repo', 'gentoo')

    # the default repo has a low priority if unset or zero
    if repos[default_repo]['priority'] == 0:
        repos[default_repo]['priority'] = -9999

    del config
    return repos


@configurable({'location': 'str'}, typename='configsection')
@errors.ParsingError.wrap_exception("while loading portage configuration")
def config_from_make_conf(location="/etc/", profile_override=None, **kwargs):
    """
    generate a config from a file location

    :param location: location the portage configuration is based in,
        defaults to /etc
    :param profile_override: profile to use instead of the current system
        profile, i.e. the target of the /etc/portage/make.profile
        (or deprecated /etc/make.profile) symlink
    """

    # this actually differs from portage parsing- we allow
    # make.globals to provide vars used in make.conf, portage keeps
    # them separate (kind of annoying)

    config_root = os.environ.get("PORTAGE_CONFIGROOT", "/")
    base_path = pjoin(config_root, location.strip("/"))
    portage_base = pjoin(base_path, "portage")

    # this isn't preserving incremental behaviour for features/use
    # unfortunately

    make_conf = {}
    try:
        load_make_conf(make_conf, pjoin(base_path, 'make.globals'))
    except errors.ParsingError as e:
        if not getattr(getattr(e, 'exc', None), 'errno', None) == errno.ENOENT:
            raise
        try:
            # fallback to defaults provided by pkgcore
            load_make_conf(make_conf, pjoin(const.CONFIG_PATH, 'make.globals'))
        except IGNORED_EXCEPTIONS:
            raise
        except:
            raise_from(errors.ParsingError(
                "failed to find a usable make.globals"))
    load_make_conf(
        make_conf, pjoin(portage_base, 'make.conf'), required=False,
        allow_sourcing=True, incrementals=True)

    root = os.environ.get("ROOT", make_conf.get("ROOT", "/"))
    gentoo_mirrors = [
        x.rstrip("/") + "/distfiles" for x in make_conf.pop("GENTOO_MIRRORS", "").split()]

    # this is flawed... it'll pick up -some-feature
    features = make_conf.get("FEATURES", "").split()

    config = {}
    triggers = []

    def add_trigger(name, kls_path, **extra_args):
        d = extra_args.copy()
        d['class'] = kls_path
        config[name] = basics.ConfigSectionFromStringDict(d)
        triggers.append(name)

    # sets...
    add_sets(config, root, portage_base)

    user_profile_path = pjoin(base_path, "portage", "profile")
    add_profile(config, base_path, user_profile_path, profile_override)

    kwds = {
        "class": "pkgcore.vdb.ondisk.tree",
        "location": pjoin(root, 'var', 'db', 'pkg'),
        "cache_location": pjoin(
            config_root, 'var', 'cache', 'edb', 'dep', 'var', 'db', 'pkg'),
    }
    config["vdb"] = basics.AutoConfigSection(kwds)

    try:
        repos_conf = load_repos_conf(pjoin(portage_base, 'repos.conf'))
    except errors.ParsingError as e:
        if not getattr(getattr(e, 'exc', None), 'errno', None) == errno.ENOENT:
            raise
        try:
            # fallback to defaults provided by pkgcore
            repos_conf = load_repos_conf(
                pjoin(const.CONFIG_PATH, 'repos.conf'))
        except IGNORED_EXCEPTIONS:
            raise
        except:
            raise_from(errors.ParsingError(
                "failed to find a usable repos.conf"))

    make_repo_syncers(config, repos_conf, make_conf)

    # sort repos via priority, in this case high values map to high priorities
    repos = [repo_opts['location'] for repo_opts in
             sorted(repos_conf.itervalues(), key=lambda d: d['priority'], reverse=True)]

    config['ebuild-repo-common'] = basics.AutoConfigSection({
        'class': 'pkgcore.ebuild.repository.slavedtree',
        'default_mirrors': gentoo_mirrors,
        'inherit-only': True,
        'ignore_paludis_versioning': ('ignore-paludis-versioning' in features),
    })

    repo_map = {}

    for repo_path in repos:
        # XXX: Hack for portage-2 profile format support.
        repo_config = RepoConfig(repo_path)
        repo_map[repo_config.repo_id] = repo_config

        # repo configs
        conf = {
            'class': 'pkgcore.ebuild.repo_objs.RepoConfig',
            'location': repo_path,
        }
        if 'sync:%s' % (repo_path,) in config:
            conf['syncer'] = 'sync:%s' % (repo_path,)
        config['raw:' + repo_path] = basics.AutoConfigSection(conf)

        # metadata cache
        cache_name = 'cache:%s' % (repo_path,)
        config[cache_name] = make_cache(config_root, repo_path)

        # repo trees
        kwds = {
            'inherit': ('ebuild-repo-common',),
            'raw_repo': ('raw:' + repo_path),
            'class': 'pkgcore.ebuild.repository.tree',
            'cache': cache_name,
        }

        config[repo_path] = basics.AutoConfigSection(kwds)

    # XXX: Hack for portage-2 profile format support. We need to figure out how
    # to dynamically create this from the config at runtime on attr access.
    profiles.ProfileNode._repo_map = ImmutableDict(repo_map)

    config['repo-stack'] = basics.FakeIncrementalDictConfigSection(
        my_convert_hybrid, {
            'class': 'pkgcore.repository.multiplex.config_tree',
            'repositories': tuple(repos)})

    config['vuln'] = basics.AutoConfigSection({
        'class': SecurityUpgradesViaProfile,
        'ebuild_repo': 'repo-stack',
        'vdb': 'vdb',
        'profile': 'profile',
    })
    config['glsa'] = basics.section_alias(
        'vuln', SecurityUpgradesViaProfile.pkgcore_config_type.typename)

    # binpkg.
    buildpkg = 'buildpkg' in features or kwargs.get('buildpkg', False)
    pkgdir = os.environ.get("PKGDIR", make_conf.pop('PKGDIR', None))
    if pkgdir is not None:
        try:
            pkgdir = abspath(pkgdir)
        except OSError as oe:
            if oe.errno != errno.ENOENT:
                raise
            if buildpkg or set(features).intersection(
                    ('pristine-buildpkg', 'buildsyspkg', 'unmerge-backup')):
                logger.warning("disabling buildpkg related features since PKGDIR doesn't exist")
            pkgdir = None
        else:
            if not ensure_dirs(pkgdir, mode=0755, minimal=True):
                logger.warning("disabling buildpkg related features since PKGDIR either doesn't "
                               "exist, or lacks 0755 minimal permissions")
                pkgdir = None
    else:
        if buildpkg or set(features).intersection(
                ('pristine-buildpkg', 'buildsyspkg', 'unmerge-backup')):
            logger.warning("disabling buildpkg related features since PKGDIR is unset")

    # yes, round two; may be disabled from above and massive else block sucks
    if pkgdir is not None:
        if pkgdir and os.path.isdir(pkgdir):
            config['binpkg'] = basics.ConfigSectionFromStringDict({
                'class': 'pkgcore.binpkg.repository.tree',
                'location': pkgdir,
                'ignore_paludis_versioning': str('ignore-paludis-versioning' in features),
            })
            repos.append('binpkg')

        if buildpkg:
            add_trigger(
                'buildpkg_trigger', 'pkgcore.merge.triggers.SavePkg',
                pristine='no', target_repo='binpkg')
        elif 'pristine-buildpkg' in features:
            add_trigger(
                'buildpkg_trigger', 'pkgcore.merge.triggers.SavePkg',
                pristine='yes', target_repo='binpkg')
        elif 'buildsyspkg' in features:
            add_trigger(
                'buildpkg_system_trigger', 'pkgcore.merge.triggers.SavePkgIfInPkgset',
                pristine='yes', target_repo='binpkg', pkgset='system')
        elif 'unmerge-backup' in features:
            add_trigger(
                'unmerge_backup_trigger', 'pkgcore.merge.triggers.SavePkgUnmerging',
                target_repo='binpkg')

    if 'save-deb' in features:
        path = make_conf.pop("DEB_REPO_ROOT", None)
        if path is None:
            logger.warning("disabling save-deb; DEB_REPO_ROOT is unset")
        else:
            add_trigger(
                'save_deb_trigger', 'pkgcore.ospkg.triggers.SaveDeb',
                basepath=normpath(path), maintainer=make_conf.pop("DEB_MAINAINER", ''),
                platform=make_conf.pop("DEB_ARCHITECTURE", ""))

    if 'splitdebug' in features:
        kwds = {}

        if 'compressdebug' in features:
            kwds['compress'] = 'true'

        add_trigger(
            'binary_debug_trigger', 'pkgcore.merge.triggers.BinaryDebug',
            mode='split', **kwds)
    elif 'strip' in features or 'nostrip' not in features:
        add_trigger(
            'binary_debug_trigger', 'pkgcore.merge.triggers.BinaryDebug',
            mode='strip')

    if '-fixlafiles' not in features:
        add_trigger(
            'lafilefixer_trigger',
            'pkgcore.system.libtool.FixLibtoolArchivesTrigger')

    # now add the fetcher- we delay it till here to clean out the environ
    # it passes to the command.
    # *everything* in make_conf must be str values also.
    distdir = normpath(os.environ.get(
        "DISTDIR", make_conf.pop("DISTDIR")))
    add_fetcher(config, make_conf, distdir)

    # finally... domain.
    make_conf.update({
        'class': 'pkgcore.ebuild.domain.domain',
        'repositories': tuple(repos),
        'fetcher': 'fetcher',
        'default': True,
        'vdb': ('vdb',),
        'profile': 'profile',
        'name': 'livefs domain',
        'root': root,
    })

    for f in ("package.mask", "package.unmask", "package.accept_keywords",
              "package.keywords", "package.license", "package.use",
              "package.env", "env:ebuild_hook_dir", "bashrc"):
        fp = pjoin(portage_base, f.split(":")[0])
        try:
            os.stat(fp)
        except OSError as oe:
            if oe.errno != errno.ENOENT:
                raise
        else:
            make_conf[f.split(":")[-1]] = fp

    if triggers:
        make_conf['triggers'] = tuple(triggers)
    config['livefs domain'] = basics.FakeIncrementalDictConfigSection(
        my_convert_hybrid, make_conf)

    return config
