# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#

import enum
import logging
from typing import Dict, Generator, List, Optional, Tuple, Callable

from volatility.framework import constants, interfaces, renderers, exceptions, symbols
from volatility.framework.configuration import requirements
from volatility.framework.interfaces import plugins, configuration
from volatility.framework.layers import scanners
from volatility.framework.renderers import format_hints
from volatility.framework.symbols import intermed
from volatility.framework.symbols.windows import extensions
from volatility.plugins.windows import handles

vollog = logging.getLogger(__name__)


# TODO: When python3.5 is no longer supported, make this enum.IntFlag
class PoolType(enum.IntEnum):
    """Class to maintain the different possible PoolTypes The values must be
    integer powers of 2."""

    PAGED = 1
    NONPAGED = 2
    FREE = 4


class PoolConstraint:
    """Class to maintain tag/size/index/type information about Pool header
    tags."""

    def __init__(self,
                 tag: bytes,
                 type_name: str,
                 object_type: Optional[str] = None,
                 page_type: Optional[PoolType] = None,
                 size: Optional[Tuple[Optional[int], Optional[int]]] = None,
                 index: Optional[Tuple[Optional[int], Optional[int]]] = None,
                 alignment: Optional[int] = 1) -> None:
        self.tag = tag
        self.type_name = type_name
        self.object_type = object_type
        self.page_type = page_type
        self.size = size
        self.index = index
        self.alignment = alignment


class PoolHeaderScanner(interfaces.layers.ScannerInterface):

    def __init__(self, module: interfaces.context.ModuleInterface, constraint_lookup: Dict[bytes, PoolConstraint],
                 alignment: int):
        super().__init__()
        self._module = module
        self._constraint_lookup = constraint_lookup
        self._alignment = alignment

        header_type = self._module.get_type('_POOL_HEADER')
        self._header_offset = header_type.relative_child_offset('PoolTag')
        self._subscanner = scanners.MultiStringScanner([c for c in constraint_lookup.keys()])

    def __call__(self, data: bytes, data_offset: int):
        for offset, pattern in self._subscanner(data, data_offset):
            header = self._module.object(object_type = "_POOL_HEADER",
                                         offset = offset - self._header_offset,
                                         absolute = True)
            constraint = self._constraint_lookup[pattern]
            try:
                # Size check
                if constraint.size is not None:
                    if constraint.size[0]:
                        if (self._alignment * header.BlockSize) < constraint.size[0]:
                            continue
                    if constraint.size[1]:
                        if (self._alignment * header.BlockSize) > constraint.size[1]:
                            continue

                # Type check
                if constraint.page_type is not None:
                    checks_pass = False

                    if (constraint.page_type & PoolType.FREE) and header.PoolType == 0:
                        checks_pass = True
                    elif (constraint.page_type & PoolType.PAGED) and header.PoolType % 2 == 0 and header.PoolType > 0:
                        checks_pass = True
                    elif (constraint.page_type & PoolType.NONPAGED) and header.PoolType % 2 == 1:
                        checks_pass = True

                    if not checks_pass:
                        continue

                if constraint.index is not None:
                    if constraint.index[0]:
                        if header.index < constraint.index[0]:
                            continue
                    if constraint.index[1]:
                        if header.index > constraint.index[1]:
                            continue
            except exceptions.InvalidAddressException:
                # The tested object's header doesn't point to valid addresses, ignore it
                continue

            # We found one that passed!
            yield (constraint, header)


def os_distinguisher(version_check: Callable[[Tuple[int, ...]], bool],
                     fallback_checks: List[Tuple[str, Optional[str], bool]]
                     ) -> Callable[[interfaces.context.ContextInterface, str], bool]:
    """Distinguishes a symbol table as being above a particular version or
    point.

    This will primarily check the version metadata first and foremost.
    If that metadata isn't available then each item in the fallback_checks is tested.
    If invert is specified then the result will be true if the version is less than that specified, or in the case of
    fallback, if any of the fallback checks is successful.

    A fallback check is made up of:
     * a symbol or type name
     * a member name (implying that the value before was a type name)
     * whether that symbol, type or member must be present or absent for the symbol table to be more above the required point

    Note:
        Specifying that a member must not be present includes the whole type not being present too (ie, either will pass the test)

    Args:
        version_check: Function that takes a 4-tuple version and returns whether whether the provided version is above a particular point
        fallback_checks: A list of symbol/types/members of types, and whether they must be present to be above the required point

    Returns:
        A function that takes a context and a symbol table name and determines whether that symbol table passes the distinguishing checks
    """

    # try the primary method based on the pe version in the ISF
    def method(context: interfaces.context.ContextInterface, symbol_table: str) -> bool:
        """
        
        Args:
            context: The context that contains the symbol table named `symbol_table`  
            symbol_table: Name of the symbol table within the context to distinguish the version of 

        Returns:
            True if the symbol table is of the required version
        """

        try:
            pe_version = context.symbol_space[symbol_table].metadata.pe_version
            major, minor, revision, build = pe_version
            return version_check((major, minor, revision, build))
        except (AttributeError, ValueError, TypeError):
            vollog.log(constants.LOGLEVEL_VVV, "Windows PE version data is not available")

        if not fallback_checks:
            raise ValueError("No fallback methods for os_distinguishing provided")

        # fall back to the backup method, if necessary
        for name, member, response in fallback_checks:
            if member is None:
                if (context.symbol_space.has_symbol(symbol_table + constants.BANG + name)
                        or context.symbol_space.has_type(symbol_table + constants.BANG + name)) != response:
                    return False
            else:
                try:
                    symbol_type = context.symbol_space.get_type(symbol_table + constants.BANG + name)
                    if symbol_type.has_member(member) != response:
                        return False
                except exceptions.SymbolError:
                    if not response:
                        return False

        return True

    return method


class PoolScanner(plugins.PluginInterface):
    """A generic pool scanner plugin."""

    _version = (1, 0, 0)

    @classmethod
    def get_requirements(cls) -> List[interfaces.configuration.RequirementInterface]:
        return [
            requirements.TranslationLayerRequirement(name = 'primary',
                                                     description = 'Memory layer for the kernel',
                                                     architectures = ["Intel32", "Intel64"]),
            requirements.SymbolTableRequirement(name = "nt_symbols", description = "Windows kernel symbols"),
            requirements.PluginRequirement(name = 'handles', plugin = handles.Handles, version = (1, 0, 0)),
        ]

    is_windows_10 = os_distinguisher(version_check = lambda x: x >= (10, 0),
                                     fallback_checks = [("ObHeaderCookie", None, True)])
    is_windows_8_or_later = os_distinguisher(version_check = lambda x: x >= (6, 2),
                                             fallback_checks = [("_HANDLE_TABLE", "HandleCount", False)])
    # Technically, this is win7 or less
    is_windows_7 = os_distinguisher(version_check = lambda x: x == (6, 1),
                                    fallback_checks = [("_OBJECT_HEADER", "TypeIndex", True),
                                                       ("_HANDLE_TABLE", "HandleCount", True)])

    def _generator(self):

        symbol_table = self.config["nt_symbols"]
        constraints = self.builtin_constraints(symbol_table)

        for constraint, mem_object, header in self.generate_pool_scan(self.context, self.config["primary"],
                                                                      symbol_table, constraints):
            # generate some type-specific info for sanity checking
            if constraint.object_type == "Process":
                name = mem_object.ImageFileName.cast("string",
                                                     max_length = mem_object.ImageFileName.vol.count,
                                                     errors = "replace")
            elif constraint.object_type == "File":
                try:
                    name = mem_object.FileName.String
                except exceptions.InvalidAddressException:
                    vollog.log(constants.LOGLEVEL_VVV, "Skipping file at {0:#x}".format(mem_object.vol.offset))
                    continue
            else:
                name = renderers.NotApplicableValue()

            yield (0, (constraint.type_name, format_hints.Hex(header.vol.offset), header.vol.layer_name, name))

    @staticmethod
    def builtin_constraints(symbol_table: str, tags_filter: List[bytes] = None) -> List[PoolConstraint]:
        """Get built-in PoolConstraints given a list of pool tags.

        The tags_filter is a list of pool tags, and the associated
        PoolConstraints are  returned. If tags_filter is empty or
        not supplied, then all builtin constraints are returned.

        Args:
            symbol_table: The name of the symbol table to prepend to the types used
            tags_filter: List of tags to return or None to return all

        Returns:
            A list of well-known constructed PoolConstraints that match the provided tags
        """

        builtins = [
            # atom tables
            PoolConstraint(b'AtmT',
                           type_name = symbol_table + constants.BANG + "_RTL_ATOM_TABLE",
                           size = (200, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # processes on windows before windows 8
            PoolConstraint(b'Pro\xe3',
                           type_name = symbol_table + constants.BANG + "_EPROCESS",
                           object_type = "Process",
                           size = (600, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # processes on windows starting with windows 8
            PoolConstraint(b'Proc',
                           type_name = symbol_table + constants.BANG + "_EPROCESS",
                           object_type = "Process",
                           size = (600, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # files on windows before windows 8
            PoolConstraint(b'Fil\xe5',
                           type_name = symbol_table + constants.BANG + "_FILE_OBJECT",
                           object_type = "File",
                           size = (150, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # files on windows starting with windows 8
            PoolConstraint(b'File',
                           type_name = symbol_table + constants.BANG + "_FILE_OBJECT",
                           object_type = "File",
                           size = (150, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # mutants on windows before windows 8
            PoolConstraint(b'Mut\xe1',
                           type_name = symbol_table + constants.BANG + "_KMUTANT",
                           object_type = "Mutant",
                           size = (64, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # mutants on windows starting with windows 8
            PoolConstraint(b'Muta',
                           type_name = symbol_table + constants.BANG + "_KMUTANT",
                           object_type = "Mutant",
                           size = (64, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # drivers on windows before windows 8
            PoolConstraint(b'Dri\xf6',
                           type_name = symbol_table + constants.BANG + "_DRIVER_OBJECT",
                           object_type = "Driver",
                           size = (248, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # drivers on windows starting with windows 8
            PoolConstraint(b'Driv',
                           type_name = symbol_table + constants.BANG + "_DRIVER_OBJECT",
                           object_type = "Driver",
                           size = (248, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # kernel modules
            PoolConstraint(b'MmLd',
                           type_name = symbol_table + constants.BANG + "_LDR_DATA_TABLE_ENTRY",
                           size = (76, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # symlinks on windows before windows 8
            PoolConstraint(b'Sym\xe2',
                           type_name = symbol_table + constants.BANG + "_OBJECT_SYMBOLIC_LINK",
                           object_type = "SymbolicLink",
                           size = (72, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # symlinks on windows starting with windows 8
            PoolConstraint(b'Symb',
                           type_name = symbol_table + constants.BANG + "_OBJECT_SYMBOLIC_LINK",
                           object_type = "SymbolicLink",
                           size = (72, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
            # registry hives
            PoolConstraint(b'CM10',
                           type_name = symbol_table + constants.BANG + "_CMHIVE",
                           size = (800, None),
                           page_type = PoolType.PAGED | PoolType.NONPAGED | PoolType.FREE),
        ]

        if not tags_filter:
            return builtins

        return [constraint for constraint in builtins if constraint.tag in tags_filter]

    @classmethod
    def generate_pool_scan(cls,
                           context: interfaces.context.ContextInterface,
                           layer_name: str,
                           symbol_table: str,
                           constraints: List[PoolConstraint]) \
            -> Generator[Tuple[
                             PoolConstraint, interfaces.objects.ObjectInterface, interfaces.objects.ObjectInterface], None, None]:
        """
        
        Args:
            context: The context to retrieve required elements (layers, symbol tables) from
            layer_name: The name of the layer on which to operate
            symbol_table: The name of the table containing the kernel symbols
            constraints: List of pool constraints used to limit the scan results 

        Returns:
            Iterable of tuples, containing the constraint that matched, the object from memory, the object header used to determine the object
        """

        # get the object type map
        type_map = handles.Handles.get_type_map(context = context, layer_name = layer_name, symbol_table = symbol_table)

        cookie = handles.Handles.find_cookie(context = context, layer_name = layer_name, symbol_table = symbol_table)

        is_windows_10 = cls.is_windows_10(context, symbol_table)
        is_windows_8_or_later = cls.is_windows_8_or_later(context, symbol_table)

        # start off with the primary virtual layer
        scan_layer = layer_name

        # switch to a non-virtual layer if necessary
        if not is_windows_10:
            scan_layer = context.layers[scan_layer].config['memory_layer']

        for constraint, header in cls.pool_scan(context, scan_layer, symbol_table, constraints, alignment = 8):

            mem_object = header.get_object(type_name = constraint.type_name,
                                           type_map = type_map,
                                           use_top_down = is_windows_8_or_later,
                                           object_type = constraint.object_type,
                                           native_layer_name = 'primary',
                                           cookie = cookie)

            if mem_object is None:
                vollog.log(constants.LOGLEVEL_VVV, "Cannot create an instance of {}".format(constraint.type_name))
                continue

            yield constraint, mem_object, header

    @classmethod
    def pool_scan(cls,
                  context: interfaces.context.ContextInterface,
                  layer_name: str,
                  symbol_table: str,
                  pool_constraints: List[PoolConstraint],
                  alignment: int = 8,
                  progress_callback: Optional[constants.ProgressCallback] = None) \
            -> Generator[Tuple[PoolConstraint, interfaces.objects.ObjectInterface], None, None]:
        """Returns the _POOL_HEADER object (based on the symbol_table template)
        after scanning through layer_name returning all headers that match any
        of the constraints provided.  Only one constraint can be provided per
        tag.

        Args:
            context: The context to retrieve required elements (layers, symbol tables) from
            layer_name: The name of the layer on which to operate
            symbol_table: The name of the table containing the kernel symbols
            pool_constraints: List of pool constraints used to limit the scan results
            alignment: An optional value that all pool headers will be aligned to
            progress_callback: An optional function to provide progress feedback whilst scanning

        Returns:
            An Iterable of pool constraints and the pool headers associated with them
        """
        # Setup the pattern
        constraint_lookup = {}  # type: Dict[bytes, PoolConstraint]
        for constraint in pool_constraints:
            if constraint.tag in constraint_lookup:
                raise ValueError("Constraint tag is used for more than one constraint: {}".format(constraint.tag))
            constraint_lookup[constraint.tag] = constraint

        module = cls._get_pool_header_module(context, layer_name, symbol_table)

        # Run the scan locating the offsets of a particular tag
        layer = context.layers[layer_name]
        scanner = PoolHeaderScanner(module, constraint_lookup, alignment)
        yield from layer.scan(context, scanner, progress_callback)

    @classmethod
    def _get_pool_header_module(cls, context, layer_name, symbol_table):
        # Setup the pool header and offset differential
        try:
            module = context.module(symbol_table, layer_name, offset = 0)
            module.get_type("_POOL_HEADER")
        except exceptions.SymbolError:
            # We have to manually load a symbol table

            if symbols.symbol_table_is_64bit(context, symbol_table):
                is_win_7 = cls.is_windows_7(context, symbol_table)
                if is_win_7:
                    pool_header_json_filename = "poolheader-x64-win7"
                else:
                    pool_header_json_filename = "poolheader-x64"
            else:
                pool_header_json_filename = "poolheader-x86"

            new_table_name = intermed.IntermediateSymbolTable.create(
                context = context,
                config_path = configuration.path_join(context.symbol_space[symbol_table].config_path, "poolheader"),
                sub_path = "windows",
                filename = pool_header_json_filename,
                table_mapping = {'nt_symbols': symbol_table},
                class_types = {'_POOL_HEADER': extensions._POOL_HEADER})
            module = context.module(new_table_name, layer_name, offset = 0)
        return module

    def run(self) -> renderers.TreeGrid:
        return renderers.TreeGrid([("Tag", str), ("Offset", format_hints.Hex), ("Layer", str), ("Name", str)],
                                  self._generator())
