# -*- coding: utf-8 -*-
# Copyright (C) 2011 Even Wiik Thomassen, Erik Bergersen,
# Sondre Johan Mannsverk, Terje Snarby, Lars Solvoll Tønder,
# Sigurd Wien and Jaroslav Fibichr.
#
# This file is part of CSjark.
#
# CSjark is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CSjark is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with CSjark.  If not, see <http://www.gnu.org/licenses/>.
"""
A module for configuration of our utility.

Should parse config files and create data structures which the parser can
use when translating C struct definitions to Wireshark protocols and fields.

Config class holds configuration for specific struct by name. FileConfig
holds C preprocessor options for specific files by path. Options holds
global utility configuration, include dictinaries for the Config and
Fileconfig instances.

Additionally there is the BaseRule class and its subclasses which holds
specific rules specified by configuration for members in structs.
"""
import sys
import os
from operator import itemgetter

import yaml

from platform import Platform
from dissector import Delegator
from field import create_lua_var, Field, BitField


class ConfigError(Exception):
    """Exception raised by invalid configuration."""
    pass


class Config:
    """Holds configuration for a specific protocol."""

    def __init__(self, name):
        """Create a new Config instance.

        'name' is the name of the struct to match.
        """
        self.name = name
        self.id = None # Message id
        self.description = None
        self.size = None # Used for unknown protocols
        self.cnf = None # Conformance File, for custom lua code
        self.members = {} # Rules for struct members
        self.types = {} # Rules for struct member types
        self.trailers = [] # Rules for protocol trailers

    def add_member_rule(self, member, rule):
        """Add a new rule for a specific member.

        'member' is the member of a struct to match
        'rule' is the new rule to add
        """
        if member not in self.members.keys():
            self.members[member] = []
        self.members[member].append(rule)

    def add_type_rule(self, type, rule):
        """Add a new rule for all members of a specific type.

        'type' is the C type to match members against
        'rule' is the new rule to add
        """
        if type not in self.types.keys():
            self.types[type] = []
        self.types[type].append(rule)

    def get_rules(self, member, type):
        """Return all rules which match 'member' or 'type'."""
        rules = self.members.get(member, [])
        rules.extend(self.types.get(type, []))
        return rules

    def create_field(self, proto, name, ctype, size, alignment, endian):
        """Create a field depending on rules."""
        # Sort the rules
        types = (Bitstring, Enum, Range, Custom)
        values = [[], [], [], []]
        for rule in self.get_rules(name, ctype):
            for i, tmp in enumerate(types):
                if isinstance(rule, tmp):
                    values[i].append(rule)
        bits, enums, ranges, customs = values

        # Custom field rules
        if customs:
            return customs[0].create(proto, name,
                    ctype, size, alignment, endian)

        # If size is None and not customs rule, we are in trouble.
        if size is None:
            size = proto.platform.size_of(ctype)
        type_ = proto.platform.map_type(ctype)

        if alignment is None:
            raise ConfigError('Unknown field alignment for %s' % name)

        # Bitstring rules
        if bits:
            return proto.add_field(BitField(bits[0].bits,
                    name, type_, size, alignment, endian))

        field = proto.add_field(Field(name, type_, size, alignment, endian))

        # Enum rules
        for rule in enums:
            field.set_list_validation(rule.values, rule.strict)

        # Range rules
        for rule in ranges:
            field.set_range_validation(rule.min, rule.max)

        return field


class BaseRule:
    """A base class for rules referring to protocol fields."""

    def __init__(self, conf, obj):
        """Read member or type info from a sub-class instance."""
        # A field rule refers either to a type or a member
        self.member = self.type = None
        if 'member' in obj:
            self.member = obj['member']
            conf.add_member_rule(self.member, self)
            del obj['member']
        elif 'type' in obj:
            self.type = obj['type']
            conf.add_type_rule(self.type, self)
            del obj['type']
        else:
            raise ConfigError('Missing either type or member declaration')


class Range(BaseRule):
    """Rule for specifying a valid range for a member or type."""

    def __init__(self, conf, obj):
        """Create a new Range rule instance."""
        super().__init__(conf, obj)

        # Min and max represents the endpoints of the valid range
        self.min = self.max = None
        if 'min' in obj:
            self.min = float(obj['min'])
        if 'max' in obj:
            self.max = float(obj['max'])
        if self.min is None and self.max is None:
            raise ConfigError('Range rule needs a min or max value.')


class Enum(BaseRule):
    """Rule for emulating enum with int-like types."""

    def __init__(self, conf, obj):
        """Create a new Enum rule instance."""
        super().__init__(conf, obj)
        self.strict = obj.get('strict', True)

        # Values is a dict which map values to enum names
        self.values = obj.get('values', None)
        if not self.values:
            raise ConfigError('Enum needs a non-empty dict or list')
        if isinstance(self.values, (list, tuple)):
            self.values = dict(enumerate(self.values))


class Bitstring(BaseRule):
    """Rule for representing ints which are bit strings."""

    def __init__(self, conf, obj):
        """Create a new Bitstring rule instance."""
        super().__init__(conf, obj)

        # Find all bitstring definitions
        self.bits = []
        for key, value in obj.items():
            # Find the bits referred to by the key
            try:
                int(key)
            except ValueError:
                if '-' not in key:
                    raise ConfigError('Invalid bitstring key: %s' % key)
                start, end = [int(i) for i in key.split('-')]
                offset = end - start + 1
            else:
                start, offset = key, 1
            if not key:
                raise ConfigError('Invalid bitstring key must be %i > 0' % key)

            # Find the bit name and values mapping
            name = value
            values = {}
            if not isinstance(value, str):
                name = value[0]
                if len(value) > 1:
                    values = dict(enumerate(value[1:]))
                elif offset == 1:
                    values = {0: 'No', 1: 'Yes'}
            elif offset == 1:
                values = {0: 'No', 1: 'Yes'}

            self.bits.append((start, offset, name, values))

        self.bits.sort(key=itemgetter(0))
        if not self.bits:
            raise ConfigError('Invalid bitstring rule for %s' % conf.name)


class Trailer(BaseRule):
    """Rule for specifying one or more trailer protocol(s)."""

    def __init__(self, conf, obj):
        """Create a new Trailer rule instance."""
        # Name of the dissector to call for the trailer
        self.name = str(obj['name'])
        if not self.name:
            raise ConfigError('Invalid trailer rule for %s' % conf.name)
        conf.trailers.append(self)

        # Count or member, which holds the amount of trailers
        self.count = self.member = None
        if 'count' in obj:
            self.count = int(obj['count'])
        if 'member' in obj:
            self.member = str(obj['member'])
        if ((self.count is None and not self.member) or
                (self.count is not None and self.member is not None)):
            raise ConfigError('Invalid trailer rule for %s' % conf.name)

        # Optional size a single trailing protocol
        self.size = None
        if 'size' in obj:
            self.size = int(obj['size'])


class Custom(BaseRule):
    """Rule for specifying a custom field handling."""

    def __init__(self, conf, obj):
        """Create a new Custom rule instance."""
        super().__init__(conf, obj)
        self.field = str(obj.get('field', ''))
        if not self.field:
            raise ConfigError('No field in Custom rule for %s' % conf.name)

        # TODO: validate that the parameters are valid for the field type
        self.size = obj.get('size', None)
        self.alignment = obj.get('alignment_size', None)
        self.abbr = obj.get('abbr', None)
        self.name = obj.get('name', None)
        self.base = obj.get('base', None)
        self.values = obj.get('values', None)
        self.mask = obj.get('mask', None)
        self.desc = obj.get('desc', None)

    def create(self, proto, name, ctype, size, alignment, endian):
        """Create a new Field based on this rule."""
        if self.size is not None:
            size = self.size
        else:
            if ctype not in proto.platform.sizes:
                raise ConfigError('Missing size for field %s' % name)
            size = proto.platform.size_of(ctype)
        if self.alignment is not None:
            alignment = self.alignment
        if alignment is None:
            alignment = size
        if self.name is not None:
            name = self.name

        # Create the field
        field = proto.add_field(Field(name,
                self.field, size, alignment, endian))
        if self.abbr is not None:
            field._abbr = self.abbr
        field.base = self.base
        if self.values:
            field.set_list_validation(self.values, strict=False)
        field.mask = self.mask
        field.desc = self.desc
        return field


class ConformanceFile:
    """A class for parsing a conformance file.

    A conformance file specifies custom lua code for fields.
    It can give custom code for the definition, and inside the dissector
    function. For these two cases, it supports header, body, footer and
    extra sections which places code above, instead of, below, or at the
    end of the section.

    Each section starts with #.<SECTION> for example #.COMMENT.
    Unknown sections are ignore, to be compatible with Asn2wrs .cnf files.
    """
    # Tokens for different sections
    t_def_hdr = 'DEF_HEADER'    # Lua code added before a field definition
    t_def_body = 'DEF_BODY'     # Lua code to replace a field definition
    t_def_ftr = 'DEF_FOOTER'    # Lua code added after a field definition
    t_def_extra = 'DEF_EXTRA'   # Lua code added after all defintions
    t_func_hdr = 'FUNC_HEADER'  # Lua code added before a field function code
    t_func_body = 'FUNC_BODY'   # Lua code to replace a field function code
    t_func_ftr = 'FUNC_FOOTER'  # Lua code added after a field function code
    t_func_extra = 'FUNC_EXTRA' # Lua code added at end of dissector function
    t_comment = 'COMMENT'       # A multiline comment section
    t_end = 'END'               # End of a section
    t_end_cnf = 'END_OF_CNF'    # End of the conformance file

    # List of all valid tokens and tokens which should store content
    def_tokens = [t_def_hdr, t_def_body, t_def_ftr]
    func_tokens = [t_func_hdr, t_func_body, t_func_ftr]
    store_tokens = def_tokens + func_tokens + [t_def_extra, t_func_extra]
    valid_tokens = store_tokens + [t_comment, t_end, t_end_cnf]

    def __init__(self, conf, file, config_file=''):
        """Parse a conformance file and create rules for it."""
        # Find the specified file
        self.file = str(file)
        if not os.path.isfile(self.file):
            self.file = os.path.join(os.path.dirname(__file__), file)
        if not os.path.isfile(self.file):
            self.file = os.path.join(os.path.dirname(config_file), file)
        if not os.path.isfile(self.file):
            raise ConfigError('Unknown file: %s' % file)

        # Read content of the specified file
        with open(self.file, 'r') as f:
            self._lines = f.readlines()

        self.rules = {}
        self.parse()

    def _get_token(self, line):
        """Find the token and the field it refers to."""
        values = line[2:].strip().split(' ') + [None]
        return values[0], values[1]

    def parse(self):
        """Parse the conformance file's sections and content."""
        token = None # Current section beeing parsed
        field = None # Field the section refers to
        content = [] # Current content for the section parsed so far

        # Go through all lines and assign content
        for line in self._lines:
            if not line.startswith('#.'):
                content.append(line.rstrip())
                continue

            # Store current content when new token is found
            if token in self.store_tokens:
                self.rules[(field, token)] = '\n'.join(content)

            content = []
            token, field = self._get_token(line)

            if token == self.t_end_cnf:
                break # End of cnf file

        # Reached end of file without an end token
        if content and token in self.store_tokens:
            self.rules[(field, token)] = '\n'.join(content)

    def match(self, name, code, definition=False, field=None):
        """Modify fields code if a cnf file demands it."""
        # Handle extra code rules
        if name is None and code is None:
            if definition:
                token = self.t_def_extra
            else:
                token = self.t_func_extra
            return self.rules.get((name, token), '')

        if definition:
            tokens = self.def_tokens
        else:
            tokens = self.func_tokens

        # Modify code if field match any rules, body first
        for token in sorted(tokens):
            content = self.rules.get((name, token), '')
            if not content:
                continue

            if not definition and field is not None:
                # Insert field offset into content
                content = content.replace('{OFFSET}', str(field.offset))

                # Insert value into content and code
                if '{VALUE}' in content:
                    variable = create_lua_var('field_value_var')
                    code = field.get_code(field.offset, store=variable)
                    content = content.replace('{VALUE}', variable)

            if token.endswith('_HEADER'):
                code = '%s\n%s' % (content, code)
            elif token.endswith('_FOOTER'):
                code = '%s\n%s' % (code, content)
            elif token.endswith('_BODY'):
                content = content.replace('%(DEFAULT_BODY)s', code)
                content = content.replace('{DEFAULT_BODY}', code)
                code = content

        return code


class FileConfig:
    """Holds options for specific files."""
    members = (
        'include_dirs', 'includes', 'defines', 'undefines', 'arguments',
    )

    def __init__(self, name):
        """Create a FileConfig which holds configuration for 'name'."""
        self.filename = name
        for var in self.members:
            setattr(self, var, [])

    def update(self, obj):
        """Update variables with config from a yml file."""
        for var in self.members:
            getattr(self, var).extend(obj.get(var, []))

    def inherit(self, parent):
        """Update variables with config from another FileConfig instance."""
        for var in self.members:
            getattr(self, var).extend(getattr(parent, var))

    @classmethod
    def add_include(cls, filename, include):
        """Add a new 'include' to 'filename' config.

        If the 'filename' has no FileConfig, creates one.
        """
        obj = Options.match_file(filename)
        if obj.filename != filename:
            obj = FileConfig(filename)
            Options.files[filename] = obj
        if obj.filename != include:
            obj.includes.append(include)


class Options:
    """Holds options for the whole utility.

    These options are set by either command line interface or
    one or more configuration yaml files.
    """
    # Parser options, can also be set by CLI
    verbose = False
    debug = False
    strict = False
    output_dir = None
    output_file = None
    generate_placeholders = False
    use_cpp = True
    cpp_path = None
    excludes = []

    # Utility options
    platforms = set() # Set of platforms to support in dissectors
    delegator = None # Used to create a delegator dissector
    configs = {} # Configuration for specific protocols
    files = {} # Cpp configuration for specific files
    default = FileConfig('default') # Default Cpp config for all files

    @classmethod
    def match_file(cls, filename):
        """Find file config object for 'filename'."""
        if filename in cls.files:
            return cls.files[filename]
        filename = os.path.basename(filename)
        return cls.files.get(filename, cls.default)

    @classmethod
    def update(cls, obj):
        """Update the options from a config yaml file."""
        # Handle platform options
        platforms = obj.get('platforms', None)
        if platforms:
            for name in platforms:
                if name in Platform.mappings:
                    cls.platforms.add(Platform.mappings[name])
                else:
                    raise ConfigError('Unknown platform %s' % name)

        # Read and update options
        members = ('verbose', 'debug', 'strict', 'output_dir',
                   'output_file', 'use_cpp', 'cpp_path')
        for member in members:
            value = obj.get(member, None)
            if value is not None:
                setattr(cls, member, value)

        # Handle exclude arguments, files and folders to NOT parse
        excludes = obj.get('excludes', None)
        if excludes:
            cls.excludes.extend(excludes)

        # Handle default C preprocessor arguments
        cls.default.update(obj)

        # Handle files configuration
        files = obj.get('files', None)
        if files:
            for file_obj in files:
                name = str(file_obj['name'])
                cls.files[name] = FileConfig(name)
                cls.files[name].update(file_obj)

    @classmethod
    def prepare_for_parsing(cls):
        """Prepare options before parsing starts.."""
        # Normalize all filename paths of interest
        def normpaths(filenames):
            return [os.path.normpath(i) for i in filenames]
        cls.excludes = normpaths(cls.excludes)
        for fconf in list(cls.files.values()) + [cls.default]:
            fconf.filename = os.path.normpath(fconf.filename)
            fconf.include_dirs = normpaths(fconf.include_dirs)
            fconf.includes = normpaths(fconf.includes)

        # Map current platform to a platform configuration
        if not cls.platforms:
            mapping = {'win': 'Win32', 'darwin': 'Macos',
                       'linux': 'Linux-x86', 'sunos': 'Solaris-x86-64'}
            for key, value in mapping.items():
                if sys.platform.startswith(key):
                    cls.platforms.add(Platform.mappings[value])

        # Add the default platform, as we failed the previous step
        if not cls.platforms:
            cls.platforms.add(Platform.mappings['default'])

        # Update file configs with the default cpp option
        for config in cls.files.values():
            config.inherit(cls.default)

        # Delegator creates lua file which delegates messages to dissectors
        cls.delegator = Delegator(Platform.mappings)

    @classmethod
    def handle_protocol_config(cls, obj, filename=''):
        """Handle rules and configuration for a protocol."""
        # Handle the name of the protocol
        name = str(obj.get('name', ''))
        if not name:
            raise ConfigError('Protocol in %s not named' % filename)

        if name not in cls.configs:
            cls.configs[name] = Config(name)
        conf = cls.configs[name]

        # Protocol's optional message id or list of message ids
        ids = obj.get('id', None)
        if ids:
            try:
                ids = [int(ids)]
            except TypeError:
                pass

            invalid_ids = [i for i in ids if i < 0 or i > 65535]
            if invalid_ids:
                raise ConfigError('Invalid dissector ID %s: %i (0 - 65535)'
                        % (conf.name, conf.id))
            conf.id = ids

        # Protocol's optional description
        description = obj.get('description', None)
        if description is not None:
            conf.description = description

        # Protocol's optional size
        size = obj.get('size', None)
        if size is not None:
            conf.size = size

        # Protocol's optional conformance file
        cnf = obj.get('cnf', None)
        if cnf:
            conf.cnf = ConformanceFile(conf, cnf, filename)

        # Handle rules
        types = {'bitstrings': Bitstring, 'enums': Enum, 'ranges': Range,
                 'trailers': Trailer, 'customs': Custom}
        for name, type_ in types.items():
            rules = obj.get(name, None)
            if rules is not None:
                for rule in rules:
                    type_(conf, rule)


def generate_placeholders(protocols):
    """Generate placeholder config for unknown structs."""
    def placeholder(proto):
        return '  - name: %s #%s' % (proto.name, proto._file)
    structs = '''
    id:
    description:
    ranges:
    enums:
    bitstrings:
    trailers:'''
    protos = {p.name: p for key, p in protocols.items()}
    data = ['%s%s\n' % (placeholder(v), structs)
            for k, v in protos.items() if k not in Options.configs]
    preample = '''\
Options:
    platforms: []
    verbose:
    debug:
    strict:
    excludes:
    use_cpp:
    cpp_path:
    default:
        include_dirs: []
        includes: []
        defines: []
        undefines: []
        arguments: []
    files:
      - name:

Structs:
'''
    return '%s%s' % (preample, '\n'.join(data)), len(data)


def parse_file(filename, only_text=None):
    """Parse a configuration file."""
    if only_text is not None:
        obj = yaml.safe_load(only_text)
    else:
        with open(filename, 'r') as f:
            obj = yaml.safe_load(f)
    if obj is None:
        return # Empty yaml file

    # Deal with utility options
    options = obj.get('Options', None)
    if options:
        Options.update(options)

    # Deal with protocol configuration
    protocols = obj.get('Structs', None)
    if protocols:
        for proto in protocols:
            Options.handle_protocol_config(proto, filename)

    if Options.verbose:
        print("Parsed config file '%s' successfully." % filename)

