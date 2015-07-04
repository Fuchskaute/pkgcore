# Copyright: 2005-2010 Brian Harring <ferringb@gmail.com>
# Copyright: 2006 Marien Zwart <marienz@gentoo.org>
# License: BSD/GPL2

"""
configuration subsystem
"""

__all__ = ("ConfigHint", "configurable", "load_config")

# keep these imports as minimal as possible; access to
# pkgcore.config isn't uncommon, thus don't trigger till
# actually needed

from snakeoil.demandload import demandload

from pkgcore.const import SYSTEM_CONF_FILE, USER_CONF_FILE

demandload(
    'os',
    'pkgcore.config:central,cparser',
    'pkgcore.ebuild.portage_conf:config_from_make_conf',
    'pkgcore.plugin:get_plugins',
)


class ConfigHint(object):

    """hint for introspection supplying overrides"""

    # be aware this is used in clone
    __slots__ = (
        "types", "positional", "required", "typename", "allow_unknowns",
        "doc", "authorative", 'requires_config')

    def __init__(self, types=None, positional=None, required=None, doc=None,
                 typename=None, allow_unknowns=False, authorative=False,
                 requires_config=False):
        self.types = types or {}
        self.positional = positional or []
        self.required = required or []
        self.typename = typename
        self.allow_unknowns = allow_unknowns
        self.doc = doc
        self.authorative = authorative
        self.requires_config = requires_config

    def clone(self, **kwds):
        new_kwds = {}
        for attr in self.__slots__:
            new_kwds[attr] = kwds.pop(attr, getattr(self, attr))
        if kwds:
            raise TypeError("unknown type overrides: %r" % kwds)
        return self.__class__(**new_kwds)


def configurable(*args, **kwargs):
    """Decorator version of ConfigHint."""
    hint = ConfigHint(*args, **kwargs)
    def decorator(original):
        original.pkgcore_config_type = hint
        return original
    return decorator


def load_config(user_conf_file=USER_CONF_FILE,
                system_conf_file=SYSTEM_CONF_FILE,
                debug=False, prepend_sources=(), append_sources=(),
                skip_config_files=False, profile_override=None,
                location=None, **kwargs):
    """
    the main entry point for any code looking to use pkgcore.

    :param user_conf_file: file to attempt to load, else defaults to trying to
        load portage configs from /etc/portage
    :param profile_override: targetted profile instead of system setting
    :param location: path to pkgcore config file or portage config directory

    :return: :obj:`pkgcore.config.central.ConfigManager` instance
        representing the system config.
    """

    configs = list(prepend_sources)
    configs.extend(get_plugins('global_config'))
    if not skip_config_files:
        for config in (location, user_conf_file, system_conf_file):
            if config is not None and os.path.isfile(config):
                with open(config) as f:
                    configs.append(cparser.config_from_file(f))
                break
        else:
            # load portage config
            configs.append(config_from_make_conf(
                location=location, profile_override=profile_override, **kwargs))
    configs.extend(append_sources)
    return central.CompatConfigManager(central.ConfigManager(configs, debug=debug))
