#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2008-2025
#  National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

"""Transformation from BooleanVar and LogicalConstraint to Binary and
Constraints."""

from pyomo.common.collections import ComponentMap
from pyomo.common.errors import MouseTrap, DeveloperError
from pyomo.common.modeling import unique_component_name
from pyomo.common.config import ConfigBlock, ConfigValue
from pyomo.contrib.fbbt.fbbt import compute_bounds_on_expr
from pyomo.core import (
    TransformationFactory,
    BooleanVar,
    VarList,
    Binary,
    LogicalConstraint,
    Block,
    ConstraintList,
    native_types,
    BooleanVarList,
    SortComponents,
)
from pyomo.core.base.block import BlockData
from pyomo.core.base.boolean_var import _DeprecatedImplicitAssociatedBinaryVariable
from pyomo.core.expr.cnf_walker import to_cnf
from pyomo.core.expr import (
    AndExpression,
    OrExpression,
    NotExpression,
    AtLeastExpression,
    AtMostExpression,
    ExactlyExpression,
    special_boolean_atom_types,
    EqualityExpression,
    InequalityExpression,
    RangedExpression,
    identify_variables,
)
from pyomo.core.expr.numvalue import native_logical_types, value
from pyomo.core.expr.visitor import StreamBasedExpressionVisitor
from pyomo.core.plugins.transform.hierarchy import IsomorphicTransformation
from pyomo.core.util import target_list


@TransformationFactory.register(
    "core.logical_to_linear", doc="Convert logic to linear constraints"
)
class LogicalToLinear(IsomorphicTransformation):
    """
    Re-encode logical constraints as linear constraints,
    converting Boolean variables to binary.
    """

    CONFIG = ConfigBlock('core.logical_to_linear')
    CONFIG.declare(
        'targets',
        ConfigValue(
            default=None,
            domain=target_list,
            description="target or list of targets that will be relaxed",
            doc="""
            This specifies the list of LogicalConstraints to transform, or the
            list of Blocks or Disjuncts on which to transform all of the
            LogicalConstraints. Note that if the transformation is done out
            of place, the list of targets should be attached to the model before it
            is cloned, and the list will specify the targets on the cloned
            instance.
            """,
        ),
    )

    def _apply_to(self, model, **kwds):
        config = self.CONFIG(kwds.pop('options', {}))
        config.set_value(kwds)
        targets = config.targets
        if targets is None:
            targets = (model,)

        new_var_lists = ComponentMap()
        transBlocks = {}
        for t in targets:
            # If the user promises that the target is Block-like, we will go
            # with it. Note, however, that they can only use targets for
            # this--when we go searching for stuff to transform we will only
            # look on Blocks. And yes, this means we are ignoring Disjuncts. We
            # are in fact ignoring all GDP components because this
            # transformation is a promise only to transform LogicalConstraints
            # and the relevant BooleanVars, not to create an algebraic
            # model. (We are making this decision largely because having this
            # transformation do anything to GDP stuff is an assumption on how
            # the GDP will be solved, and it would be wrong to assume that a GDP
            # will *necessarily* be solved as an algebraic model. The star
            # example of not doing so being GDPopt.)
            if t.ctype is Block or isinstance(t, BlockData):
                self._transform_block(t, model, new_var_lists, transBlocks)
            elif t.ctype is LogicalConstraint:
                if t.is_indexed():
                    self._transform_constraint(t, new_var_lists, transBlocks)
                else:
                    self._transform_constraintData(t, new_var_lists, transBlocks)
            else:
                raise RuntimeError(
                    "Target '%s' was not a Block, Disjunct, or"
                    " LogicalConstraint. It was of type %s "
                    "and can't be transformed." % (t.name, type(t))
                )

    def _transform_boolean_varData(self, bool_vardata, new_varlists):
        # This transformation tries to group the binaries it creates for indexed
        # BooleanVars onto the same VarList. This won't work across separate
        # calls to the transformation, but within one call it's fine. So we have
        # two cases: 1) either we have created a VarList for this
        # BooleanVarData's parent_component, but have yet to add its binary to
        # said list, or 2) we have neither the binary nor the VarList

        parent_component = bool_vardata.parent_component()
        new_varlist = new_varlists.get(parent_component)
        if new_varlist is None and bool_vardata.get_associated_binary() is None:
            # Case 2) we have neither the VarList nor an associated binary
            parent_block = bool_vardata.parent_block()
            new_var_list_name = unique_component_name(
                parent_block, parent_component.local_name + '_asbinary'
            )
            new_varlist = VarList(domain=Binary)
            setattr(parent_block, new_var_list_name, new_varlist)
            new_varlists[parent_component] = new_varlist

        if bool_vardata.get_associated_binary() is None:
            # Case 1) we already have a VarList, but need to create the
            # associated binary
            new_binary_vardata = new_varlist.add()
            bool_vardata.associate_binary_var(new_binary_vardata)
            if bool_vardata.value is not None:
                new_binary_vardata.value = int(bool_vardata.value)
            if bool_vardata.fixed:
                new_binary_vardata.fix()

    def _transform_constraint(self, constraint, new_varlists, transBlocks):
        for i in constraint.keys(sort=SortComponents.ORDERED_INDICES):
            self._transform_constraintData(constraint[i], new_varlists, transBlocks)
        constraint.deactivate()

    def _transform_block(self, target_block, model, new_varlists, transBlocks):
        _blocks = (
            target_block.values() if target_block.is_indexed() else (target_block,)
        )
        for block in _blocks:
            for logical_constraint in block.component_objects(
                ctype=LogicalConstraint, active=True, descend_into=Block
            ):
                self._transform_constraint(
                    logical_constraint, new_varlists, transBlocks
                )

            # This can go away when we deprecate this transformation
            # transforming BooleanVars. This just marks the BooleanVars as
            # "seen" so that if someone asks for their binary var later, we can
            # create it on the fly and complain.
            for bool_vardata in block.component_data_objects(
                BooleanVar, descend_into=Block
            ):
                if bool_vardata._associated_binary is None:
                    bool_vardata._associated_binary = (
                        _DeprecatedImplicitAssociatedBinaryVariable(bool_vardata)
                    )

    def _transform_constraintData(self, logical_constraint, new_varlists, transBlocks):
        # first find all the relevant BooleanVars and associate a binary (if
        # they don't have one already)
        for bool_vardata in identify_variables(logical_constraint.expr):
            if bool_vardata.ctype is BooleanVar:
                self._transform_boolean_varData(bool_vardata, new_varlists)

        # now create a transformation block on the constraint's parent block (if
        # we don't have one already)
        parent_block = logical_constraint.parent_block()
        xfrm_block = transBlocks.get(parent_block)
        if xfrm_block is None:
            xfrm_block = self._create_transformation_block(parent_block)
            transBlocks[parent_block] = xfrm_block
        new_constrlist = xfrm_block.transformed_constraints
        new_boolvarlist = xfrm_block.augmented_vars
        new_varlist = xfrm_block.augmented_vars_asbinary

        old_boolvarlist_length = len(new_boolvarlist)

        indicator_map = ComponentMap()
        cnf_statements = to_cnf(logical_constraint.body, new_boolvarlist, indicator_map)
        logical_constraint.deactivate()

        # Associate new Boolean vars to new binary variables
        num_new = len(new_boolvarlist) - old_boolvarlist_length
        list_o_vars = list(new_boolvarlist.values())
        if num_new:
            for bool_vardata in list_o_vars[-num_new:]:
                new_binary_vardata = new_varlist.add()
                bool_vardata.associate_binary_var(new_binary_vardata)

        # Add constraints associated with each CNF statement
        for cnf_statement in cnf_statements:
            for linear_constraint in _cnf_to_linear_constraint_list(cnf_statement):
                new_constrlist.add(expr=linear_constraint)

        # Add bigM associated with special atoms
        # Note: this ad-hoc reformulation may be revisited for tightness in the
        # future.
        old_varlist_length = len(new_varlist)
        for indicator_var, special_atom in indicator_map.items():
            for linear_constraint in _cnf_to_linear_constraint_list(
                special_atom, indicator_var, new_varlist
            ):
                new_constrlist.add(expr=linear_constraint)

        # Previous step may have added auxiliary binaries. Associate augmented
        # Booleans to them.
        num_new = len(new_varlist) - old_varlist_length
        list_o_vars = list(new_varlist.values())
        if num_new:
            for binary_vardata in list_o_vars[-num_new:]:
                new_bool_vardata = new_boolvarlist.add()
                new_bool_vardata.associate_binary_var(binary_vardata)

    def _create_transformation_block(self, context):
        new_xfrm_block_name = unique_component_name(context, 'logic_to_linear')
        new_xfrm_block = Block(doc="Transformation objects for logic_to_linear")
        setattr(context, new_xfrm_block_name, new_xfrm_block)

        new_xfrm_block.transformed_constraints = ConstraintList()
        new_xfrm_block.augmented_vars = BooleanVarList()
        new_xfrm_block.augmented_vars_asbinary = VarList(domain=Binary)

        return new_xfrm_block


def update_boolean_vars_from_binary(model, integer_tolerance=1e-5):
    """Updates all Boolean variables based on the value of their linked binary
    variables."""
    for boolean_var in model.component_data_objects(BooleanVar, descend_into=Block):
        binary_var = boolean_var.get_associated_binary()
        if binary_var is not None and binary_var.value is not None:
            if abs(binary_var.value - 1) <= integer_tolerance:
                boolean_var.value = True
            elif abs(binary_var.value) <= integer_tolerance:
                boolean_var.value = False
            else:
                raise ValueError(
                    "Binary variable has non-{0,1} value: "
                    "%s = %s" % (binary_var.name, binary_var.value)
                )
            boolean_var.stale = binary_var.stale


def _cnf_to_linear_constraint_list(cnf_expr, indicator_var=None, binary_varlist=None):
    # Screen for constants
    if type(cnf_expr) in native_types or cnf_expr.is_constant():
        if value(cnf_expr) is True:
            # Trivially feasible: no constraints
            return []
        else:
            # Trivially infeasible: we will return an infeasible
            # constant expression, because if we are nested within
            # something like a Disjunct, the model may still be feasible
            # (only this disjunct is not feasible).
            return [InequalityExpression((1, 0), False)]
    if cnf_expr.is_expression_type():
        return CnfToLinearVisitor(indicator_var, binary_varlist).walk_expression(
            cnf_expr
        )
    else:
        return [cnf_expr.get_associated_binary() == 1]  # Assume that cnf_expr
        # is a BooleanVar


_numeric_relational_types = {InequalityExpression, EqualityExpression, RangedExpression}


class CnfToLinearVisitor(StreamBasedExpressionVisitor):
    """Convert CNF logical constraint to linear constraints.

    Expected expression node types: AndExpression, OrExpression, NotExpression,
    AtLeastExpression, AtMostExpression, ExactlyExpression, BooleanVarData

    """

    def __init__(self, indicator_var, binary_varlist):
        super(CnfToLinearVisitor, self).__init__()
        self._indicator = indicator_var
        self._binary_varlist = binary_varlist

    def exitNode(self, node, values):
        if type(node) == AndExpression:
            return list(
                (v if type(v) in _numeric_relational_types else v == 1) for v in values
            )
        elif type(node) == OrExpression:
            return sum(values) >= 1
        elif type(node) == NotExpression:
            return 1 - values[0]
        # Note: the following special atoms should only be encountered as root
        # nodes.  If they are encountered otherwise, something went wrong.
        sum_values = sum(values[1:])
        num_args = node.nargs() - 1  # number of logical arguments
        if self._indicator is None:
            if type(node) == AtLeastExpression:
                return sum_values >= values[0]
            elif type(node) == AtMostExpression:
                return sum_values <= values[0]
            elif type(node) == ExactlyExpression:
                return sum_values == values[0]
        else:
            rhs_lb, rhs_ub = compute_bounds_on_expr(values[0])
            if rhs_lb == float('-inf') or rhs_ub == float('inf'):
                raise ValueError(
                    "Cannot generate linear constraints for %s"
                    "([N, *logical_args]) with unbounded N. "
                    "Detected %s <= N <= %s." % (type(node).__name__, rhs_lb, rhs_ub)
                )
            indicator_binary = self._indicator.get_associated_binary()
            if type(node) == AtLeastExpression:
                return [
                    sum_values >= values[0] - rhs_ub * (1 - indicator_binary),
                    sum_values
                    <= values[0] - 1 + (-(rhs_lb - 1) + num_args) * indicator_binary,
                ]
            elif type(node) == AtMostExpression:
                return [
                    sum_values
                    <= values[0] + (-rhs_lb + num_args) * (1 - indicator_binary),
                    sum_values >= (values[0] + 1) - (rhs_ub + 1) * indicator_binary,
                ]
            elif type(node) == ExactlyExpression:
                less_than_binary = self._binary_varlist.add()
                more_than_binary = self._binary_varlist.add()
                return [
                    sum_values
                    <= values[0] + (-rhs_lb + num_args) * (1 - indicator_binary),
                    sum_values >= values[0] - rhs_ub * (1 - indicator_binary),
                    indicator_binary + less_than_binary + more_than_binary >= 1,
                    sum_values
                    <= values[0]
                    - 1
                    + (-(rhs_lb - 1) + num_args) * (1 - less_than_binary),
                    sum_values >= values[0] + 1 - (rhs_ub + 1) * (1 - more_than_binary),
                ]
        if type(node) in _numeric_relational_types:
            raise MouseTrap(
                "core.logical_to_linear does not support transforming "
                "LogicalConstraints with embedded relational expressions.  "
                f"Found '{node}'."
            )
        else:
            raise DeveloperError(
                f"Unsupported node type {type(node)} encountered when "
                f"transforming a CNF expression to its linear equivalent ({node})."
            )

    def beforeChild(self, node, child, child_idx):
        if type(node) in special_boolean_atom_types and child is node.args[0]:
            return False, child
        if type(child) in native_logical_types:
            return False, int(child)
        if type(child) in native_types:
            return False, child

        if child.is_expression_type():
            return True, None

        # Only thing left should be BooleanVarData
        #
        # TODO: After the expr_multiple_dispatch is merged, this should
        # be switched to using as_numeric.
        if hasattr(child, 'get_associated_binary'):
            return False, child.get_associated_binary()
        else:
            return False, child

    def finalizeResult(self, result):
        if type(result) is list:
            return result
        elif type(result) in _numeric_relational_types:
            return [result]
        else:
            return [result == 1]
