"""
A module for parsing C files, and searching AST for struct definitions.

Requires PLY and pycparser.
"""
import sys
import os

import pycparser
from pycparser import c_ast, c_parser, plyparser

from config import size_of, map_type, StructConfig
from dissector import Protocol


class ParseError(plyparser.ParseError):
    """Exception raised by invalid input to the parser."""
    pass


def parse_file(filename, use_cpp=True, fake_includes=True, cpp_path=None):
    """Parse a C file, returns abstract syntax tree.

    use_cpp: Enable or disable the C preprocessor
    fake_includes: Add fake includes for libc header files
    cpp_path: The path to cpp.exe on windows
    """
    cpp_args = None
    if cpp_path is None:
        cpp_path = 'cpp'

    if use_cpp:
        cpp_args = []

        if fake_includes:
            cpp_args.append(r'-I../utils/fake_libc_include')

        if os.path.dirname(filename):
            cpp_args.append(r'-I%s' % os.path.dirname(filename))

        # TODO: find a cleaner way to look for cpp on windows!
        if sys.platform == 'win32' and cpp_path == 'cpp':
            cpp_path = '../utils/cpp.exe' # Windows don't come with a CPP
        elif sys.platform == 'darwin':
            cpp_path = 'gcc' # Fix for a bug in Mac GCC 4.2.1
            cpp_args.append('-E')

    # Generate an abstract syntax tree
    ast = pycparser.parse_file(filename, use_cpp=use_cpp,
            cpp_path=cpp_path, cpp_args=cpp_args)

    return ast


def parse(text, filename=''):
    """Parse C code and return an AST."""
    parser = c_parser.CParser()
    return parser.parse(text, filename)


def find_structs(ast):
    """Walks the AST nodes to find structs."""
    visitor = StructVisitor()
    visitor.visit(ast)
    return list(visitor.structs.values())


class StructVisitor(c_ast.NodeVisitor):
    """A class which visit struct nodes in the AST."""

    all_struct_names = {} # Map struct names and their coords

    def __init__(self):
        self.structs = {} # All structs encountered in this AST
        self.enums = {} # All enums encountered in this AST
        self.aliases = {} # Typedefs and their base type
        self.type_decl = [] # Queue of current type declaration

    def _get_type(self, node):
        """Get the C type from a node."""
        return ' '.join(reversed(node.names))

    def visit_Struct(self, node):
        """Visit a Struct node in the AST."""
        # Visit children
        c_ast.NodeVisitor.generic_visit(self, node)

        # No children, its a member and not a declaration
        if not node.children():
            return

        # Typedef structs
        if not node.name:
            node.name = self.type_decl[-1]

        # Create the protocol for the struct
        conf = StructConfig.configs.get(node.name, None)
        proto = Protocol(node.name, node.coord, conf)

        # Find the member definitions
        for decl in node.children():
            child = decl.children()[0]

            if isinstance(child, c_ast.TypeDecl):
                self.handle_type_decl(child, proto)
            elif isinstance(child, c_ast.ArrayDecl):
                self.handle_array_decl(child, proto)
            elif isinstance(child, c_ast.PtrDecl):
                self.handle_ptr_decl(child, proto)
            else:
                raise ParseError('Unknown struct member: %s' % repr(child))

        # Disallow structs with same name
        if node.name in StructVisitor.all_struct_names:
            o = StructVisitor.all_struct_names[node.name]
            if (os.path.normpath(o.file) != os.path.normpath(node.coord.file)
                    or o.line != node.coord.line):
                raise ParseError('Two structs with same name %s: %s:%i & %s:%i' % (
                       node.name, o.file, o.line, node.coord.file, node.coord.line))
        else:
            StructVisitor.all_struct_names[node.name] = node.coord

        # Don't add protocols with no fields? Sounds reasonably
        if proto.fields:
            self.structs[node.name] = proto

    def visit_Enum(self, node):
        """Visit a Enum node in the AST."""
        # Visit children
        c_ast.NodeVisitor.generic_visit(self, node)

        # Empty Enum definition or using Enum
        if not node.children():
            return

        # Find id:name of members
        members = {}
        i = -1
        for child in node.children()[0].children():
            if child.children():
                i = int(child.children()[0].value)
            else:
                i += 1
            members[i] = child.name

        self.enums[node.name] = members

    def visit_Typedef(self, node):
        """Visit Typedef declarations nodes in the AST."""
        # Visit children
        c_ast.NodeVisitor.generic_visit(self, node)

        # Find the type
        child = node.children()[0].children()[0]
        if isinstance(child, c_ast.IdentifierType):
            type = self._get_type(child)
            type = self.aliases.get(type, type)
            self.aliases[node.name] = type

    def visit_TypeDecl(self, node):
        """Keep track of Type Declaration nodes."""
        self.type_decl.append(node.declname)
        c_ast.NodeVisitor.generic_visit(self, node)
        self.type_decl.pop()

    def handle_type_decl(self, node, proto):
        """Find member details in a type declaration."""
        child = node.children()[0]
        if isinstance(child, c_ast.IdentifierType):
            ctype = self._get_type(child)
            self.add_field(proto, node.declname, ctype)
        elif isinstance(child, c_ast.Enum):
            if child.name not in self.enums.keys():
                raise ParseError('Unknown enum: %s' % child.name)
            type, size = map_type('enum'), size_of('enum')
            proto.add_enum(node.declname, type, size, self.enums[child.name])
        elif isinstance(child, c_ast.Union):
            self.add_field(proto, node.declname, 'union')
        elif isinstance(child, c_ast.Struct):
            subproto = self.structs[child.name]
            size = subproto.get_size()
            proto.add_protocol(node.declname, subproto.id, size, child.name)
        else:
            raise ParseError('Unknown type declaration: %s' % repr(child))

    def _get_array_size(self, node):
        """Calculate the size of the array."""
        child = node.children()[1]

        if isinstance(child, c_ast.Constant):
            size = int(child.value)
        elif isinstance(child, c_ast.BinaryOp):
            size = 0 # TODO: evaluate BinaryOp expression
        elif isinstance(child, c_ast.ID):
            size = 0 # TODO: PATH_MAX WTF?
        else:
            raise ParseError('This type of array not supported: %s' % node)

        return size

    def handle_array_decl(self, node, proto, depth=None):
        """Find member details in an array declaration."""
        if depth is None:
            depth = []
        child = node.children()[0]
        size = self._get_array_size(node)

        # String array
        if (isinstance(child, c_ast.TypeDecl) and
                child.children()[0].names[0] == 'char'):
            type = map_type('string')
            size *= size_of('char')
            if depth:
                proto.add_array(child.declname, type, size, depth)
            else:
                self.add_field(proto, child.declname, type, size)
            return

        # Multidimensional, handle recursively
        if isinstance(child, c_ast.ArrayDecl):
            if size > 1:
                depth.append(size)
            self.handle_array_decl(child, proto, depth)

        # Single dimensional normal array
        else:
            depth.append(size)
            ctype = self._get_type(child.children()[0])
            size = size_of(ctype)
            proto.add_array(child.declname, map_type(ctype), size, depth)

    def handle_ptr_decl(self, node, proto):
        """Find member details in a pointer declaration."""
        self.add_field(proto, node.children()[0].declname, 'pointer')

    def add_field(self, proto, name, ctype, size=None):
        """Add a field representing the struct member to the protocol."""
        if size is None:
            size = size_of(ctype)
        if proto.conf is None:
            proto.add_field(name, map_type(ctype), size)
        else:
            proto.conf.create_field(proto, name, ctype, size)

