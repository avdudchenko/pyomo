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
#
# Test NEOS solver interface
#
# Because the Kestrel tests require connections to the NEOS server, and
# that can take quite a while (5-20+ seconds), we will only run these
# tests as part of the nightly suite (i.e., by the CI system as part of
# PR / master tests)
#

import os
import json
import os.path

import pyomo.common.unittest as unittest
from pyomo.common.log import LoggingIntercept

from pyomo.scripting.pyomo_main import main
from pyomo.scripting.util import cleanup
from pyomo.neos.kestrel import kestrelAMPL
import pyomo.neos

import pyomo.environ as pyo

from pyomo.common.fileutils import this_file_dir

currdir = this_file_dir()

neos_available = False
try:
    if kestrelAMPL().neos is not None:
        neos_available = True
except:
    pass

email_set = True
if os.environ.get('NEOS_EMAIL') is None:
    email_set = False


def _model(sense):
    # Goals of this model:
    # - linear
    # - solution has nonzero variable values (so they appear in the results)
    model = pyo.ConcreteModel()
    model.y = pyo.Var(bounds=(-10, 10), initialize=0.5)
    model.x = pyo.Var(bounds=(-5, 5), initialize=0.5)

    @model.ConstraintList()
    def c(m):
        yield m.y >= m.x - 2
        yield m.y >= -m.x
        yield m.y <= m.x
        yield m.y <= 2 - m.x

    model.obj = pyo.Objective(expr=model.y, sense=sense)
    return model


@unittest.pytest.mark.default
@unittest.pytest.mark.neos
@unittest.skipIf(not neos_available, "Cannot make connection to NEOS server")
@unittest.skipUnless(email_set, "NEOS_EMAIL not set")
class TestKestrel(unittest.TestCase):
    def test_doc(self):
        kestrel = kestrelAMPL()
        tmp = [tuple(name.split(':')) for name in kestrel.solvers()]
        amplsolvers = set(v[0].lower() for v in tmp if v[1] == 'AMPL')

        doc = pyomo.neos.doc
        dockeys = set(doc.keys())

        self.assertEqual(amplsolvers, dockeys)

        # gamssolvers = set(v[0].lower() for v in tmp if v[1]=='GAMS')
        # missing = gamssolvers - amplsolvers
        # self.assertEqual(len(missing) == 0)

    def test_connection_failed(self):
        try:
            orig_host = pyomo.neos.kestrel.NEOS.host
            pyomo.neos.kestrel.NEOS.host = 'neos-bogus-server.org'
            with LoggingIntercept() as LOG:
                kestrel = kestrelAMPL()
            self.assertIsNone(kestrel.neos)
            self.assertRegex(
                LOG.getvalue(), r"NEOS is temporarily unavailable:\n\t\(.+\)"
            )
        finally:
            pyomo.neos.kestrel.NEOS.host = orig_host

    def test_check_all_ampl_solvers(self):
        kestrel = kestrelAMPL()
        solvers = kestrel.getAvailableSolvers()
        for solver in solvers:
            name = solver.lower().replace('-', '')
            if not hasattr(RunAllNEOSSolvers, 'test_' + name):
                self.fail(f"RunAllNEOSSolvers missing test for '{solver}'")


class RunAllNEOSSolvers(object):
    def test_baron(self):
        self._run('baron')

    def test_bonmin(self):
        self._run('bonmin')

    def test_cbc(self):
        self._run('cbc')

    def test_conopt(self):
        self._run('conopt')

    def test_couenne(self):
        self._run('couenne')

    def test_cplex(self):
        self._run('cplex')

    def test_filmint(self):
        self._run('filmint')

    def test_filter(self):
        self._run('filter')

    def test_ipopt(self):
        self._run('ipopt')

    def test_knitro(self):
        self._run('knitro')

    # This solver only handles bound constrained variables
    def test_lbfgsb(self):
        self._run('l-bfgs-b', False)

    def test_lancelot(self):
        self._run('lancelot')

    def test_loqo(self):
        self._run('loqo')

    def test_minlp(self):
        self._run('minlp')

    def test_minos(self):
        self._run('minos')

    def test_minto(self):
        self._run('minto')

    def test_mosek(self):
        self._run('mosek')

    # [16 Jul 24]: Octeract is erroring.  We will disable the interface
    # (and testing) until we have time to resolve #3321
    # [20 Sep 24]: and appears to have been removed from NEOS
    # [24 Apr 25]: it appears to be there but causes timeouts
    def test_octeract(self):
        pass
        # self._run('octeract')

    def test_ooqp(self):
        if self.sense == pyo.maximize:
            # OOQP does not recognize maximization problems and
            # minimizes instead.
            with self.assertRaisesRegex(AssertionError, '.* != 1 within'):
                self._run('ooqp')
        else:
            self._run('ooqp')

    def test_path(self):
        # The simple tests aren't complementarity problems
        self.skipTest("The simple NEOS test is not a complementarity problem")

    def test_snopt(self):
        self._run('snopt')

    def test_raposa(self):
        self._run('raposa')

    def test_lgo(self):
        self._run('lgo')


class DirectDriver(object):
    def _run(self, opt, constrained=True):
        m = _model(self.sense)
        with pyo.SolverManagerFactory('neos') as solver_manager:
            results = solver_manager.solve(m, opt=opt)

        expected_y = {
            (pyo.minimize, True): -1,
            (pyo.maximize, True): 1,
            (pyo.minimize, False): -10,
            (pyo.maximize, False): 10,
        }[self.sense, constrained]

        self.assertEqual(results.solver[0].status, pyo.SolverStatus.ok)
        if constrained:
            # If the solver ignores constraints, x is degenerate
            self.assertAlmostEqual(pyo.value(m.x), 1, delta=1e-5)
        self.assertAlmostEqual(pyo.value(m.obj), expected_y, delta=1e-5)
        self.assertAlmostEqual(pyo.value(m.y), expected_y, delta=1e-5)


class PyomoCommandDriver(object):
    def _run(self, opt, constrained=True):
        expected_y = {
            (pyo.minimize, True): -1,
            (pyo.maximize, True): 1,
            (pyo.minimize, False): -10,
            (pyo.maximize, False): 10,
        }[self.sense, constrained]

        filename = (
            'model_min_lp.py' if self.sense == pyo.minimize else 'model_max_lp.py'
        )

        results = os.path.join(currdir, 'result.json')
        args = [
            'solve',
            os.path.join(currdir, filename),
            '--solver-manager=neos',
            '--solver=%s' % opt,
            '--logging=quiet',
            '--save-results=%s' % results,
            '--results-format=json',
            '-c',
        ]
        try:
            output = main(args)
            self.assertEqual(output.errorcode, 0)

            with open(results) as FILE:
                data = json.load(FILE)
        finally:
            cleanup()
            if os.path.exists(results):
                os.remove(results)

        self.assertEqual(data['Solver'][0]['Status'], 'ok')
        self.assertEqual(data['Solution'][1]['Status'], 'optimal')
        self.assertAlmostEqual(
            data['Solution'][1]['Objective']['obj']['Value'], expected_y, delta=1e-5
        )
        if constrained:
            # If the solver ignores constraints, x is degenerate
            self.assertAlmostEqual(
                data['Solution'][1]['Variable']['x']['Value'], 1, delta=1e-5
            )
        self.assertAlmostEqual(
            data['Solution'][1]['Variable']['y']['Value'], expected_y, delta=1e-5
        )


@unittest.pytest.mark.neos
@unittest.skipIf(not neos_available, "Cannot make connection to NEOS server")
@unittest.skipUnless(email_set, "NEOS_EMAIL not set")
class TestSolvers_direct_call_min(RunAllNEOSSolvers, DirectDriver, unittest.TestCase):
    sense = pyo.minimize


@unittest.pytest.mark.neos
@unittest.skipIf(not neos_available, "Cannot make connection to NEOS server")
@unittest.skipUnless(email_set, "NEOS_EMAIL not set")
class TestSolvers_direct_call_max(RunAllNEOSSolvers, DirectDriver, unittest.TestCase):
    sense = pyo.maximize


@unittest.pytest.mark.neos
@unittest.skipIf(not neos_available, "Cannot make connection to NEOS server")
@unittest.skipUnless(email_set, "NEOS_EMAIL not set")
class TestSolvers_pyomo_cmd_min(
    RunAllNEOSSolvers, PyomoCommandDriver, unittest.TestCase
):
    sense = pyo.minimize


@unittest.pytest.mark.default
@unittest.skipIf(not neos_available, "Cannot make connection to NEOS server")
@unittest.skipUnless(email_set, "NEOS_EMAIL not set")
class TestCBC_timeout_direct_call(DirectDriver, unittest.TestCase):
    sense = pyo.minimize

    @unittest.timeout(60, timeout_raises=unittest.SkipTest)
    def test_cbc_timeout(self):
        super()._run('cbc')


@unittest.pytest.mark.default
@unittest.skipIf(not neos_available, "Cannot make connection to NEOS server")
@unittest.skipUnless(email_set, "NEOS_EMAIL not set")
class TestCBC_timeout_pyomo_cmd(PyomoCommandDriver, unittest.TestCase):
    sense = pyo.minimize

    @unittest.timeout(60, timeout_raises=unittest.SkipTest)
    def test_cbc_timeout(self):
        super()._run('cbc')


if __name__ == "__main__":
    unittest.main()
