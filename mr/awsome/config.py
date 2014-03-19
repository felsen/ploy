from mr.awsome.common import Hooks
from ConfigParser import RawConfigParser
from UserDict import DictMixin
from weakref import proxy
import os
import warnings


def value_asbool(value):
    if value.lower() in ('true', 'yes', 'on'):
        return True
    elif value.lower() in ('false', 'no', 'off'):
        return False


class BaseMassager(object):
    def __init__(self, sectiongroupname, key):
        self.sectiongroupname = sectiongroupname
        self.key = key

    def __call__(self, config, sectionname):
        return config._dict[self.key]


class BooleanMassager(BaseMassager):
    def __call__(self, config, sectionname):
        value = BaseMassager.__call__(self, config, sectionname)
        value = value_asbool(value)
        if value is None:
            raise ValueError("Unknown value %s for %s in %s:%s." % (value, self.key, self.sectiongroupname, sectionname))
        return value


class IntegerMassager(BaseMassager):
    def __call__(self, config, sectionname):
        value = BaseMassager.__call__(self, config, sectionname)
        return int(value)


class PathMassager(BaseMassager):
    def __call__(self, config, sectionname):
        value = BaseMassager.__call__(self, config, sectionname)
        value = os.path.expanduser(value)
        if not os.path.isabs(value):
            value = os.path.join(config._config.path, value)
        return value


def resolve_dotted_name(value):
    if '.' in value:
        prefix, name = value.rsplit('.', 1)
        _temp = __import__(prefix, globals(), locals(), [name], -1)
        return getattr(_temp, name)
    else:
        return __import__(value, globals(), locals(), [], -1)


class HooksMassager(BaseMassager):
    def __call__(self, config, sectionname):
        value = BaseMassager.__call__(self, config, sectionname)
        hooks = Hooks()
        for hook_spec in value.split():
            hooks.add(resolve_dotted_name(hook_spec)())
        return hooks


class MassagersMassager(BaseMassager):
    def __call__(self, config, sectionname):
        value = BaseMassager.__call__(self, config, sectionname)
        massagers = []
        for spec in value.split('\n'):
            spec = spec.strip()
            if not spec:
                continue
            key, massager = spec.split('=')
            sectiongroupname, key = tuple(x.strip() for x in key.split(':'))
            massager = resolve_dotted_name(massager.strip())
            massagers.append(massager(sectiongroupname, key))
        return massagers


class StartupScriptMassager(BaseMassager):
    def __call__(self, config, sectionname):
        value = BaseMassager.__call__(self, config, sectionname)
        result = dict()
        if value.startswith('gzip:'):
            value = value[5:]
            result['gzip'] = True
        if not os.path.isabs(value):
            value = os.path.join(config._config.path, value)
        result['path'] = value
        return result


class UserMassager(BaseMassager):
    def __call__(self, config, sectionname):
        value = BaseMassager.__call__(self, config, sectionname)
        if value == "*":
            import pwd
            value = pwd.getpwuid(os.getuid())[0]
        return value


class ConfigSection(DictMixin):
    def __init__(self, *args, **kw):
        self._dict = dict(*args, **kw)
        self.sectionname = None
        self.sectiongroupname = None
        self._config = None
        self.massagers = {}

    def add_massager(self, massager):
        key = (massager.sectiongroupname, massager.key)
        if key in self.massagers:
            raise ValueError("Massager for option '%s' in section group '%s' already registered." % (massager.key, massager.sectiongroupname))
        self.massagers[key] = massager

    def __delitem__(self, key):
        del self._dict[key]

    def __getitem__(self, key):
        if key == '__groupname__':
            return self.sectiongroupname
        if key == '__name__':
            return self.sectionname
        if key in self._dict:
            if self._config is not None:
                massage = self._config.massagers.get((self.sectiongroupname, key))
                if not callable(massage):
                    massage = self._config.massagers.get((None, key))
                    if callable(massage):
                        return massage(self, self.sectiongroupname, self.sectionname)
                else:
                    return massage(self, self.sectionname)
            massage = self.massagers.get((self.sectiongroupname, key))
            if callable(massage):
                return massage(self, self.sectionname)
        return self._dict[key]

    def __setitem__(self, key, value):
        self._dict[key] = value

    def keys(self):
        return self._dict.keys()

    def copy(self):
        new = ConfigSection()
        new._dict = self._dict.copy()
        new.sectionname = self.sectionname
        new.sectiongroupname = self.sectiongroupname
        new._config = self._config
        return new


class Config(ConfigSection):
    def _expand(self, sectiongroupname, sectionname, section, seen):
        if (sectiongroupname, sectionname) in seen:
            raise ValueError("Circular macro expansion.")
        seen.add((sectiongroupname, sectionname))
        macronames = section['<'].split()
        for macroname in macronames:
            if ':' in macroname:
                macrogroupname, macroname = macroname.split(':')
            else:
                macrogroupname = sectiongroupname
            macro = self[macrogroupname][macroname]
            if '<' in macro:
                self._expand(macrogroupname, macroname, macro, seen)
            if sectiongroupname in self.macro_cleaners:
                macro = dict(macro)
                self.macro_cleaners[sectiongroupname](macro)
            for key in macro:
                if key not in section:
                    section[key] = macro[key]
        # this needs to be after the recursive _expand call, so circles are
        # properly detected
        del section['<']

    def __init__(self, config, path=None, bbb_config=False, plugins=None):
        ConfigSection.__init__(self)
        self.config = config
        if path is None:
            if getattr(config, 'read', None) is None:
                path = os.path.dirname(config)
        self.path = path
        self.macro_cleaners = {}
        if plugins is not None:
            for plugin in plugins.values():
                for massager in plugin.get('get_massagers', lambda: [])():
                    self.add_massager(massager)
                if 'get_macro_cleaners' in plugin:
                    self.macro_cleaners.update(plugin['get_macro_cleaners'](self))

    def parse(self):
        _config = RawConfigParser()
        _config.optionxform = lambda s: s
        if getattr(self.config, 'read', None) is not None:
            _config.readfp(self.config)
        else:
            _config.read(self.config)
        for configsection in _config.sections():
            if ':' in configsection:
                sectiongroupname, sectionname = configsection.split(':')
            else:
                sectiongroupname, sectionname = 'global', configsection
            sectiongroup = self.setdefault(sectiongroupname, ConfigSection())
            if sectionname not in sectiongroup:
                section = ConfigSection()
                section.sectiongroupname = sectiongroupname
                section.sectionname = sectionname
                section._config = proxy(self)
                sectiongroup[sectionname] = section
            sectiongroup[sectionname].update(_config.items(configsection))
        if 'plugin' in self:
            warnings.warn("The 'plugin' section isn't used anymore.")
            del self['plugin']
        seen = set()
        for sectiongroupname in self:
            sectiongroup = self[sectiongroupname]
            for sectionname in sectiongroup:
                section = sectiongroup[sectionname]
                if '<' in section:
                    self._expand(sectiongroupname, sectionname, section, seen)
                if 'massagers' in section:
                    massagers = MassagersMassager(
                        sectiongroupname,
                        'massagers')(self, sectionname)
                    for massager in massagers:
                        self.add_massager(massager)
        return self

    def get_section_with_overrides(self, sectiongroupname, sectionname, overrides):
        config = self[sectiongroupname][sectionname].copy()
        if overrides is not None:
            config._dict.update(overrides)
        return config
