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

from pyomo.core import (
    Var,
    Block,
    Constraint,
    Param,
    Set,
    SetOf,
    Suffix,
    Expression,
    Objective,
    SortComponents,
    value,
    ConstraintList,
)
from pyomo.core.base import TransformationFactory, VarData
from pyomo.core.plugins.transform.hierarchy import Transformation
from pyomo.common.config import ConfigBlock, ConfigValue, NonNegativeFloat
from pyomo.common.modeling import unique_component_name
from pyomo.repn.standard_repn import generate_standard_repn
from pyomo.common.collections import ComponentMap, ComponentSet
from pyomo.opt import TerminationCondition
from pyomo.util.config_domains import ComponentDataSet

import logging

logger = logging.getLogger('pyomo.contrib.fme')


def _check_var_bounds_filter(constraint):
    """Check if the constraint is already implied by the variable bounds"""
    # this is one of our constraints, so we know that it is >=.
    min_lhs = 0
    for v, coef in constraint['map'].items():
        if coef > 0:
            if v.lb is None:
                return True  # we don't have var bounds with which to imply the
                # constraint...
            min_lhs += coef * v.lb
        elif coef < 0:
            if v.ub is None:
                return True  # we don't have var bounds with which to imply the
                # constraint...
            min_lhs += coef * v.ub
    # we do need value here since we didn't control v.lb and v.ub above.
    if value(min_lhs) >= constraint['lower']:
        return False  # constraint implied by var bounds
    return True


def gcd(a, b):
    while b != 0:
        a, b = b, a % b
    return abs(a)


def lcm(ints):
    a = ints[0]
    for b in ints[1:]:
        a = abs(a * b) // gcd(a, b)
    return a


@TransformationFactory.register(
    'contrib.fourier_motzkin_elimination',
    doc="Project out specified (continuous) variables from a linear model.",
)
class Fourier_Motzkin_Elimination_Transformation(Transformation):
    """Project out specified variables from a linear model.

    This transformation requires the following keyword argument:
        vars_to_eliminate: A user-specified list of continuous variables to
                           project out of the model

    The transformation will deactivate the original constraints of the model
    and create a new block named "_pyomo_contrib_fme_transformation" with the
    projected constraints. Note that this transformation will flatten the
    structure of the original model since there is no obvious mapping between
    the original model and the transformed one.

    """

    CONFIG = ConfigBlock("contrib.fourier_motzkin_elimination")
    CONFIG.declare(
        'vars_to_eliminate',
        ConfigValue(
            default=None,
            domain=ComponentDataSet(Var),
            description="Continuous variable or list of continuous variables to "
            "project out of the model",
            doc="""
            This specifies the list of variables to project out of the model.
            Note that these variables must all be continuous and the model must be
            linear.""",
        ),
    )
    CONFIG.declare(
        'constraint_filtering_callback',
        ConfigValue(
            default=_check_var_bounds_filter,
            description="A callback that determines whether or not new "
            "constraints generated by Fourier-Motzkin elimination are added "
            "to the model",
            doc="""
            Specify None in order for no constraint filtering to occur during the
            transformation.
    
            Specify a function that accepts a constraint (represented in the >=
            dictionary form used in this transformation) and returns a Boolean
            indicating whether or not to add it to the model.
            """,
        ),
    )
    CONFIG.declare(
        'do_integer_arithmetic',
        ConfigValue(
            default=False,
            domain=bool,
            description="A Boolean flag to decide whether Fourier-Motzkin "
            "elimination will be performed with only integer arithmetic.",
            doc="""
            If True, only integer arithmetic will be performed during Fourier-
            Motzkin elimination. This should result in no numerical error.
            If True and there is non-integer data in the constraints being
            projected, an error will be raised.
    
            If False, the algorithm will not check whether data is integer, and will
            perform division operations. Use this setting when not all data is
            integer, or when you are willing to sacrifice some numeric accuracy.
            """,
        ),
    )
    CONFIG.declare(
        'verbose',
        ConfigValue(
            default=False,
            domain=bool,
            description="A Boolean flag to enable verbose output.",
            doc="""
            If True, logs the steps of the projection.
            """,
        ),
    )
    CONFIG.declare(
        'zero_tolerance',
        ConfigValue(
            default=0,
            domain=NonNegativeFloat,
            description="Absolute tolerance at which a float will be considered 0.",
            doc="""
            Whenever fourier-motzkin elimination is used with non-integer data,
            there is a chance of numeric trouble, the most obvious of which is
            that 'eliminated' variables will remain in the constraints with very
            small coefficients. Set this tolerance so that floating points smaller
            than this will be treated as 0 (and reported that way in the final
            constraints).
            """,
        ),
    )
    CONFIG.declare(
        'integer_tolerance',
        ConfigValue(
            default=0,
            domain=NonNegativeFloat,
            description="Absolute tolerance at which a float will be considered "
            "(and cast to) an integer, when do_integer_arithmetic is True",
            doc="""
            Tolerance at which a number x will be considered an integer, when we
            are performing fourier-motzkin elimination with only integer_arithmetic.
            That is, x will be cast to an integer if
            abs(int(x) - x) <= integer_tolerance.
            """,
        ),
    )
    CONFIG.declare(
        'projected_constraints_name',
        ConfigValue(
            default=None,
            domain=str,
            description="Optional name for the ConstraintList containing the "
            "projected constraints. Must be a unique name with respect to the "
            "instance.",
            doc="""
            Optional name for the ConstraintList containing the projected 
            constraints. If not specified, the constraints will be stored on a 
            private block created by the transformation, so if you want access 
            to them after the transformation, use this argument.
    
            Must be a string which is a unique component name with respect to the 
            Block on which the transformation is called.
            """,
        ),
    )

    def __init__(self):
        """Initialize transformation object"""
        super(Fourier_Motzkin_Elimination_Transformation, self).__init__()

    def _apply_to(self, instance, **kwds):
        log_level = logger.level
        try:
            config = self.CONFIG(kwds.pop('options', {}))
            config.set_value(kwds)
            # lower logging values emit more
            if config.verbose and log_level > logging.INFO:
                logger.setLevel(logging.INFO)
                self.verbose = True
            # if the user used the logger to ask for info level messages
            elif log_level <= logging.INFO:
                self.verbose = True
            else:
                self.verbose = False
            self._apply_to_impl(instance, config)
        finally:
            # restore logging level
            logger.setLevel(log_level)

    def _apply_to_impl(self, instance, config):
        vars_to_eliminate = config.vars_to_eliminate
        self.constraint_filter = config.constraint_filtering_callback
        self.do_integer_arithmetic = config.do_integer_arithmetic
        self.integer_tolerance = config.integer_tolerance
        self.zero_tolerance = config.zero_tolerance
        if vars_to_eliminate is None:
            raise RuntimeError(
                "The Fourier-Motzkin Elimination transformation "
                "requires the argument vars_to_eliminate, a "
                "list of Vars to be projected out of the model."
            )

        # make transformation block
        transBlockName = unique_component_name(
            instance, '_pyomo_contrib_fme_transformation'
        )
        transBlock = Block()
        instance.add_component(transBlockName, transBlock)
        nm = config.projected_constraints_name
        if nm is None:
            projected_constraints = transBlock.projected_constraints = ConstraintList()
        else:
            # check that this component doesn't already exist
            if instance.component(nm) is not None:
                raise RuntimeError(
                    "projected_constraints_name was specified "
                    "as '%s', but this is already a component "
                    "on the instance! Please specify a unique "
                    "name." % nm
                )
            projected_constraints = ConstraintList()
            instance.add_component(nm, projected_constraints)

        # collect all of the constraints
        # NOTE that we are ignoring deactivated constraints
        constraints = []
        ctypes_not_to_transform = set(
            (Block, Param, Objective, Set, SetOf, Expression, Suffix, Var)
        )
        for obj in instance.component_data_objects(
            descend_into=Block, sort=SortComponents.deterministic, active=True
        ):
            if obj.ctype in ctypes_not_to_transform:
                continue
            elif obj.ctype is Constraint:
                cons_list = self._process_constraint(obj)
                constraints.extend(cons_list)
                obj.deactivate()  # the truth will be on our transformation block
            else:
                raise RuntimeError(
                    "Found active component %s of type %s. The "
                    "Fourier-Motzkin Elimination transformation can only "
                    "handle purely algebraic models. That is, only "
                    "Sets, Params, Vars, Constraints, Expressions, Blocks, "
                    "and Objectives may be active on the model." % (obj.name, obj.ctype)
                )

        for obj in vars_to_eliminate:
            if obj.lb is not None:
                constraints.append(
                    {
                        'body': generate_standard_repn(obj),
                        'lower': value(obj.lb),
                        'map': ComponentMap([(obj, 1)]),
                    }
                )
            if obj.ub is not None:
                constraints.append(
                    {
                        'body': generate_standard_repn(-obj),
                        'lower': -value(obj.ub),
                        'map': ComponentMap([(obj, -1)]),
                    }
                )

        new_constraints = self._fourier_motzkin_elimination(
            constraints, vars_to_eliminate
        )

        # put the new constraints on the transformation block
        for cons in new_constraints:
            if self.constraint_filter is not None:
                try:
                    keep = self.constraint_filter(cons)
                except:
                    logger.error(
                        "Problem calling constraint filter callback "
                        "on constraint with right-hand side %s and "
                        "body:\n%s" % (cons['lower'], cons['body'].to_expression())
                    )
                    raise
                if not keep:
                    continue
            lhs = cons['body'].to_expression(sort=True)
            lower = cons['lower']
            assert type(lower) is int or type(lower) is float
            if type(lhs >= lower) is bool:
                if lhs >= lower:
                    continue
                else:
                    # This would actually make a lot of sense in this case...
                    # projected_constraints.add(Constraint.Infeasible)
                    raise RuntimeError("Fourier-Motzkin found the model is infeasible!")
            else:
                projected_constraints.add(lhs >= lower)

    def _process_constraint(self, constraint):
        """Transforms a pyomo Constraint object into a list of dictionaries
        representing only >= constraints. That is, if the constraint has both an
        ub and a lb, it is transformed into two constraints. Otherwise it is
        flipped if it is <=. Each dictionary contains the keys 'lower',
        and 'body' where, after the process, 'lower' will be a constant, and
        'body' will be the standard repn of the body. (The constant will be
        moved to the RHS and we know that the upper bound is None after this).
        """
        body = constraint.body
        std_repn = generate_standard_repn(body)
        # make sure that we store the lower bound's value so that we need not
        # worry again during the transformation
        cons_dict = {'lower': value(constraint.lower), 'body': std_repn}
        upper = value(constraint.upper)
        constraints_to_add = [cons_dict]
        if upper is not None:
            # if it has both bounds
            if cons_dict['lower'] is not None:
                # copy the constraint and flip
                leq_side = {
                    'lower': -upper,
                    'body': generate_standard_repn(-1.0 * body),
                }
                self._move_constant_and_add_map(leq_side)
                constraints_to_add.append(leq_side)

            # If it has only an upper bound, we just need to flip it
            else:
                # just flip the constraint
                cons_dict['lower'] = -upper
                cons_dict['body'] = generate_standard_repn(-1.0 * body)
        self._move_constant_and_add_map(cons_dict)

        return constraints_to_add

    def _move_constant_and_add_map(self, cons_dict):
        """Takes constraint in dictionary form already in >= form,
        and moves the constant to the RHS
        """
        body = cons_dict['body']
        constant = value(body.constant)
        cons_dict['lower'] -= constant
        body.constant = 0

        # store a map of vars to coefficients. We can't use this in place of
        # standard repn because determinism, but this will save a lot of linear
        # time searches later. Note also that we will take the value of the
        # coefficient here so that we never have to worry about it again during
        # the transformation.
        cons_dict['map'] = ComponentMap(
            zip(body.linear_vars, [value(coef) for coef in body.linear_coefs])
        )

    def _fourier_motzkin_elimination(self, constraints, vars_to_eliminate):
        """Performs FME on the constraint list in the argument
        (which is assumed to be all >= constraints and stored in the
        dictionary representation), projecting out each of the variables in
        vars_to_eliminate"""

        # We only need to eliminate variables that actually appear in
        # this set of constraints... Revise our list.
        vars_that_appear = []
        vars_that_appear_set = ComponentSet()
        for cons in constraints:
            std_repn = cons['body']
            if not std_repn.is_linear():
                # as long as none of vars_that_appear are in the nonlinear part,
                # we are actually okay.
                nonlinear_vars = ComponentSet(
                    v for two_tuple in std_repn.quadratic_vars for v in two_tuple
                )
                nonlinear_vars.update(v for v in std_repn.nonlinear_vars)
                for var in nonlinear_vars:
                    if var in vars_to_eliminate:
                        raise RuntimeError(
                            "Variable %s appears in a nonlinear "
                            "constraint. The Fourier-Motzkin "
                            "Elimination transformation can only "
                            "be used to eliminate variables "
                            "which only appear linearly." % var.name
                        )
            for var in std_repn.linear_vars:
                if var in vars_to_eliminate:
                    if not var in vars_that_appear_set:
                        vars_that_appear.append(var)
                        vars_that_appear_set.add(var)

        # we actually begin the recursion here
        total = len(vars_that_appear)
        iteration = 1
        while vars_that_appear:
            # first var we will project out
            the_var = vars_that_appear.pop()
            logger.warning("Projecting out var %s of %s" % (iteration, total))
            if self.verbose:
                logger.info("Projecting out %s" % the_var.getname(fully_qualified=True))
                logger.info("New constraints are:")

            # we are 'reorganizing' the constraints, we sort based on the sign
            # of the coefficient of the_var: This tells us whether we have
            # the_var <= other stuff or vice versa.
            leq_list = []
            geq_list = []
            waiting_list = []

            coefs = []
            for cons in constraints:
                leaving_var_coef = cons['map'].get(the_var)
                if leaving_var_coef is None or leaving_var_coef == 0:
                    waiting_list.append(cons)
                    if self.verbose:
                        logger.info(
                            "\t%s <= %s" % (cons['lower'], cons['body'].to_expression())
                        )
                    continue

                # we know the constraint is a >= constraint, using that
                # assumption below.
                # NOTE: neither of the scalar multiplications below flip the
                # constraint. So we are sure to have only geq constraints
                # forever, which is exactly what we want.
                if not self.do_integer_arithmetic:
                    if leaving_var_coef < 0:
                        leq_list.append(
                            self._nonneg_scalar_multiply_linear_constraint(
                                cons, -1.0 / leaving_var_coef
                            )
                        )
                    else:
                        geq_list.append(
                            self._nonneg_scalar_multiply_linear_constraint(
                                cons, 1.0 / leaving_var_coef
                            )
                        )
                else:
                    coefs.append(
                        self._as_integer(
                            leaving_var_coef,
                            self._get_noninteger_coef_error_message,
                            (the_var.name, leaving_var_coef),
                        )
                    )
            if self.do_integer_arithmetic and len(coefs) > 0:
                least_common_mult = lcm(coefs)
                for cons in constraints:
                    leaving_var_coef = cons['map'].get(the_var)
                    if leaving_var_coef is None or leaving_var_coef == 0:
                        continue
                    to_lcm = least_common_mult // abs(int(leaving_var_coef))
                    if leaving_var_coef < 0:
                        leq_list.append(
                            self._nonneg_scalar_multiply_linear_constraint(cons, to_lcm)
                        )
                    else:
                        geq_list.append(
                            self._nonneg_scalar_multiply_linear_constraint(cons, to_lcm)
                        )

            constraints = waiting_list
            for leq in leq_list:
                for geq in geq_list:
                    constraints.append(self._add_linear_constraints(leq, geq))
                    if self.verbose:
                        cons = constraints[len(constraints) - 1]
                        logger.info(
                            "\t%s <= %s" % (cons['lower'], cons['body'].to_expression())
                        )

            iteration += 1

        return constraints

    def _get_noninteger_coef_error_message(self, varname, coef):
        return (
            "The do_integer_arithmetic flag was "
            "set to True, but the coefficient of "
            "%s is non-integer within the specified "
            "tolerance, with value %s. \n"
            "Please set do_integer_arithmetic="
            "False, increase integer_tolerance, "
            "or make your data integer." % (varname, coef)
        )

    def _as_integer(self, x, error_message, error_args):
        if abs(int(x) - x) <= self.integer_tolerance:
            return int(round(x))
        raise ValueError(
            error_message if error_args is None else error_message(*error_args)
        )

    def _multiply(self, scalar, coef, error_message, error_args):
        if self.do_integer_arithmetic:
            assert type(scalar) is int
            return scalar * self._as_integer(coef, error_message, error_args)
        elif abs(scalar * coef) > self.zero_tolerance:
            return scalar * coef
        else:
            return 0

    def _add(self, a, b, error_message, error_args):
        if self.do_integer_arithmetic:
            return self._as_integer(a, error_message, error_args) + self._as_integer(
                b, error_message, error_args
            )
        elif abs(a + b) > self.zero_tolerance:
            return a + b
        else:
            return 0

    def _nonneg_scalar_multiply_linear_constraint_error_msg(self, cons, coef):
        return (
            "The do_integer_arithmetic flag was set to True, but the "
            "lower bound of %s is non-integer within the specified "
            "tolerance, with value %s. \n"
            "Please set do_integer_arithmetic=False, increase "
            "integer_tolerance, or make your data integer."
            % (cons['body'].to_expression() >= cons['lower'], coef)
        )

    def _nonneg_scalar_multiply_linear_constraint(self, cons, scalar):
        """Multiplies all coefficients and the RHS of a >= constraint by scalar.
        There is no logic for flipping the equality, so this is just the
        special case with a nonnegative scalar, which is all we need.

        If self.do_integer_arithmetic is True, this assumes that scalar is an
        int. It also will throw an error if any data is non-integer (within
        tolerance)
        """
        body = cons['body']
        new_coefs = []
        for i, coef in enumerate(body.linear_coefs):
            v = body.linear_vars[i]
            new_coefs.append(
                self._multiply(
                    scalar,
                    coef,
                    self._get_noninteger_coef_error_message,
                    (v.name, coef),
                )
            )
            # update the map
            cons['map'][v] = new_coefs[i]
        body.linear_coefs = new_coefs

        body.quadratic_coefs = [scalar * coef for coef in body.quadratic_coefs]
        body.nonlinear_expr = (
            scalar * body.nonlinear_expr if body.nonlinear_expr is not None else None
        )

        # assume scalar >= 0 and constraint only has lower bound
        lb = cons['lower']
        if lb is not None:
            cons['lower'] = self._multiply(
                scalar,
                lb,
                self._nonneg_scalar_multiply_linear_constraint_error_msg,
                (cons, coef),
            )
        return cons

    def _add_linear_constraints_error_msg(self, cons1, cons2):
        return (
            "The do_integer_arithmetic flag was set to True, but while "
            "adding %s and %s, encountered a coefficient that is "
            "non-integer within the specified tolerance\n"
            "Please set do_integer_arithmetic=False, increase "
            "integer_tolerance, or make your data integer."
            % (
                cons1['body'].to_expression() >= cons1['lower'],
                cons2['body'].to_expression() >= cons2['lower'],
            )
        )

    def _add_linear_constraints(self, cons1, cons2):
        """Adds two >= constraints

        Because this is always called after
        _nonneg_scalar_multiply_linear_constraint, though it is implemented
        more generally.
        """
        ans = {'lower': None, 'body': None, 'map': ComponentMap()}
        cons1_body = cons1['body']
        cons2_body = cons2['body']

        # Need this to be both deterministic and to account for the fact that
        # Vars aren't hashable.
        all_vars = list(cons1_body.linear_vars)
        seen = ComponentSet(all_vars)
        for v in cons2_body.linear_vars:
            if v not in seen:
                all_vars.append(v)

        expr = 0
        for var in all_vars:
            coef = self._add(
                cons1['map'].get(var, 0),
                cons2['map'].get(var, 0),
                self._add_linear_constraints_error_msg,
                (cons1, cons2),
            )
            ans['map'][var] = coef
            expr += coef * var

        # deal with nonlinear stuff if there is any
        for cons in [cons1_body, cons2_body]:
            if cons.nonlinear_expr is not None:
                expr += cons.nonlinear_expr
            expr += sum(
                coef * v1 * v2
                for (coef, (v1, v2)) in zip(cons.quadratic_coefs, cons.quadratic_vars)
            )

        ans['body'] = generate_standard_repn(expr)

        # upper is None and lower exists, so this gets the constant
        ans['lower'] = self._add(
            cons1['lower'],
            cons2['lower'],
            self._add_linear_constraints_error_msg,
            (cons1, cons2),
        )

        return ans

    def post_process_fme_constraints(
        self, m, solver_factory, projected_constraints=None, tolerance=0
    ):
        """Function that solves a sequence of LPs problems to check if
        constraints are implied by each other. Deletes any that are.

        Parameters
        ----------------
        m: A model, already transformed with FME. Note that if constraints
           have been added, activated, or deactivated, we will check for
           redundancy against the whole active part of the model. If you call
           this straight after FME, you are only checking within the projected
           constraints, but otherwise it is up to the user.
        solver_factory: A SolverFactory object (constructed with a solver
                        which can solve the continuous relaxation of the
                        active constraints on the model. That is, if you
                        had nonlinear constraints unrelated to the variables
                        being projected, you need to either deactivate them or
                        provide a solver which will do the right thing.)
        projected_constraints: The ConstraintList of projected constraints.
                               Default is None, in which case we assume that
                               the FME transformation was called without
                               specifying their name, so will look for them on
                               the private transformation block.
        tolerance: Tolerance at which we decide a constraint is implied by the
                   others. Default is 0, meaning we remove the constraint if
                   the LP solve finds the constraint can be tight but not
                   violated. Setting this to a small positive value would
                   remove constraints more conservatively. Setting it to a
                   negative value would result in a relaxed problem.
        """
        if projected_constraints is None:
            # make sure m looks like what we expect
            if not hasattr(m, "_pyomo_contrib_fme_transformation"):
                raise RuntimeError(
                    "It looks like model %s has not been "
                    "transformed with the "
                    "fourier_motzkin_elimination transformation!" % m.name
                )
            transBlock = m._pyomo_contrib_fme_transformation
            if not hasattr(transBlock, 'projected_constraints'):
                raise RuntimeError(
                    "It looks the projected constraints "
                    "were manually named when the FME "
                    "transformation was called on %s. "
                    "If this is so, specify the ConstraintList "
                    "of projected constraints with the "
                    "'projected_constraints' argument." % m.name
                )
            projected_constraints = transBlock.projected_constraints

        # relax integrality so that we can do this with LP solves.
        TransformationFactory('core.relax_integer_vars').apply_to(
            m, transform_deactivated_blocks=True
        )
        # deactivate any active objectives on the model, and save what we did so
        # we can undo it after.
        active_objs = []
        for obj in m.component_data_objects(Objective, descend_into=True):
            if obj.active:
                active_objs.append(obj)
            obj.deactivate()
        # add placeholder for our own objective
        obj_name = unique_component_name(m, '_fme_post_process_obj')
        obj = Objective(expr=0)
        m.add_component(obj_name, obj)
        for i in projected_constraints:
            # If someone wants us to ignore it and leave it in the model, we
            # can.
            if not projected_constraints[i].active:
                continue
            # deactivate the constraint
            projected_constraints[i].deactivate()
            # Our constraint looks like: 0 <= a^Tx - b, so make objective to
            # maximize its infeasibility
            obj.expr = projected_constraints[i].body - projected_constraints[i].lower
            results = solver_factory.solve(m)
            if results.solver.termination_condition == TerminationCondition.unbounded:
                obj_val = -float('inf')
            elif results.solver.termination_condition != TerminationCondition.optimal:
                raise RuntimeError(
                    "Unsuccessful subproblem solve when checking"
                    "constraint %s.\n\t"
                    "Termination Condition: %s"
                    % (
                        projected_constraints[i].name,
                        results.solver.termination_condition,
                    )
                )
            else:
                obj_val = value(obj)
            # if we couldn't make it infeasible, it's useless
            if obj_val >= tolerance:
                del projected_constraints[i]
            else:
                projected_constraints[i].activate()

        # clean up
        m.del_component(obj)
        for obj in active_objs:
            obj.activate()
        # undo relax integrality
        TransformationFactory('core.relax_integer_vars').apply_to(m, undo=True)
