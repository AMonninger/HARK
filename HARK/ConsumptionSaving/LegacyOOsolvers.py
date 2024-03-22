"""
This file contains code for legacy object-oriented solvers. In version 0.15.0 of
HARK, the OO solvers (solve_one_period functions) that had been used for years
were replaced with simpler single function solvers. To preserve legacy functionality
for users with downstream projects, the OO solvers have been moved to this file,
and it should be possible to substitute them back into the appropriate AgentTypes.
"""

from copy import deepcopy
import numpy as np
from HARK import MetricObject, NullFunc
from HARK.distribution import expected
from HARK.interpolation import (
    BilinearInterp,
    CubicInterp,
    IdentityFunction,
    LinearInterp,
    LinearInterpOnInterp1D,
    LowerEnvelope,
    MargMargValueFuncCRRA,
    MargValueFuncCRRA,
    ValueFuncCRRA,
)
from HARK.rewards import (
    UtilityFuncCRRA,
    UtilityFuncStoneGeary,
)
from HARK.ConsumptionSaving.ConsIndShockModel import (
    ConsumerSolution,
    utility,
    utility_inv,
    utility_invP,
    utilityP,
    utilityP_inv,
)
from HARK.ConsumptionSaving.ConsPortfolioModel import PortfolioSolution


class ConsPerfForesightSolver(MetricObject):
    """
    A class for solving a one period perfect foresight
    consumption-saving problem.
    An instance of this class is created by the function solvePerfForesight
    in each period.

    Parameters
    ----------
    solution_next : ConsumerSolution
        The solution to next period's one-period problem.
    DiscFac : float
        Intertemporal discount factor for future utility.
    LivPrb : float
        Survival probability; likelihood of being alive at the beginning of
        the next period.
    CRRA : float
        Coefficient of relative risk aversion.
    Rfree : float
        Risk free interest factor on end-of-period assets.
    PermGroFac : float
        Expected permanent income growth factor at the end of this period.
    BoroCnstArt : float or None
        Artificial borrowing constraint, as a multiple of permanent income.
        Can be None, indicating no artificial constraint.
    MaxKinks : int
        Maximum number of kink points to allow in the consumption function;
        additional points will be thrown out.  Only relevant in infinite
        horizon model with artificial borrowing constraint.
    """

    def __init__(
        self,
        solution_next,
        DiscFac,
        LivPrb,
        CRRA,
        Rfree,
        PermGroFac,
        BoroCnstArt,
        MaxKinks,
    ):
        self.solution_next = solution_next
        self.DiscFac = DiscFac
        self.LivPrb = LivPrb
        self.CRRA = CRRA
        self.Rfree = Rfree
        self.PermGroFac = PermGroFac
        self.BoroCnstArt = BoroCnstArt
        self.MaxKinks = MaxKinks

    def def_utility_funcs(self):
        """
        Defines CRRA utility function for this period (and its derivatives),
        saving them as attributes of self for other methods to use.

        Parameters
        ----------
        None

        Returns
        -------
        None
        """
        self.u = UtilityFuncCRRA(self.CRRA)

    def def_value_funcs(self):
        """
        Defines the value and marginal value functions for this period.
        Uses the fact that for a perfect foresight CRRA utility problem,
        if the MPC in period t is :math:`\\kappa_{t}`, and relative risk
        aversion :math:`\\rho`, then the inverse value vFuncNvrs has a
        constant slope of :math:`\\kappa_{t}^{-\\rho/(1-\\rho)}` and
        vFuncNvrs has value of zero at the lower bound of market resources
        mNrmMin.  See PerfForesightConsumerType.ipynb documentation notebook
        for a brief explanation and the links below for a fuller treatment.

        https://www.econ2.jhu.edu/people/ccarroll/public/lecturenotes/consumption/PerfForesightCRRA/#vFuncAnalytical
        https://www.econ2.jhu.edu/people/ccarroll/SolvingMicroDSOPs/#vFuncPF

        Parameters
        ----------
        None

        Returns
        -------
        None
        """

        # See the PerfForesightConsumerType.ipynb documentation notebook for the derivations
        vFuncNvrsSlope = self.MPCmin ** (-self.CRRA / (1.0 - self.CRRA))
        vFuncNvrs = LinearInterp(
            np.array([self.mNrmMinNow, self.mNrmMinNow + 1.0]),
            np.array([0.0, vFuncNvrsSlope]),
        )
        self.vFunc = ValueFuncCRRA(vFuncNvrs, self.CRRA)
        self.vPfunc = MargValueFuncCRRA(self.cFunc, self.CRRA)

    def make_cFunc_PF(self):
        """
        Makes the (linear) consumption function for this period.

        Parameters
        ----------
        None

        Returns
        -------
        None
        """
        # Use a local value of BoroCnstArt to prevent comparing None and float below.
        if self.BoroCnstArt is None:
            BoroCnstArt = -np.inf
        else:
            BoroCnstArt = self.BoroCnstArt

        # Calculate human wealth this period
        self.hNrmNow = (self.PermGroFac / self.Rfree) * (self.solution_next.hNrm + 1.0)

        # Calculate the lower bound of the marginal propensity to consume
        PatFac = ((self.Rfree * self.DiscFacEff) ** (1.0 / self.CRRA)) / self.Rfree
        self.MPCmin = 1.0 / (1.0 + PatFac / self.solution_next.MPCmin)

        # Extract the discrete kink points in next period's consumption function;
        # don't take the last one, as it only defines the extrapolation and is not a kink.
        mNrmNext = self.solution_next.cFunc.x_list[:-1]
        cNrmNext = self.solution_next.cFunc.y_list[:-1]

        # Calculate the end-of-period asset values that would reach those kink points
        # next period, then invert the first order condition to get consumption. Then
        # find the endogenous gridpoint (kink point) today that corresponds to each kink
        aNrmNow = (self.PermGroFac / self.Rfree) * (mNrmNext - 1.0)
        cNrmNow = (self.DiscFacEff * self.Rfree) ** (-1.0 / self.CRRA) * (
            self.PermGroFac * cNrmNext
        )
        mNrmNow = aNrmNow + cNrmNow

        # Add an additional point to the list of gridpoints for the extrapolation,
        # using the new value of the lower bound of the MPC.
        mNrmNow = np.append(mNrmNow, mNrmNow[-1] + 1.0)
        cNrmNow = np.append(cNrmNow, cNrmNow[-1] + self.MPCmin)

        # If the artificial borrowing constraint binds, combine the constrained and
        # unconstrained consumption functions.
        if BoroCnstArt > mNrmNow[0]:
            # Find the highest index where constraint binds
            cNrmCnst = mNrmNow - BoroCnstArt
            CnstBinds = cNrmCnst < cNrmNow
            idx = np.where(CnstBinds)[0][-1]

            if idx < (mNrmNow.size - 1):
                # If it is not the *very last* index, find the the critical level
                # of mNrm where the artificial borrowing contraint begins to bind.
                d0 = cNrmNow[idx] - cNrmCnst[idx]
                d1 = cNrmCnst[idx + 1] - cNrmNow[idx + 1]
                m0 = mNrmNow[idx]
                m1 = mNrmNow[idx + 1]
                alpha = d0 / (d0 + d1)
                mCrit = m0 + alpha * (m1 - m0)

                # Adjust the grids of mNrm and cNrm to account for the borrowing constraint.
                cCrit = mCrit - BoroCnstArt
                mNrmNow = np.concatenate(([BoroCnstArt, mCrit], mNrmNow[(idx + 1) :]))
                cNrmNow = np.concatenate(([0.0, cCrit], cNrmNow[(idx + 1) :]))

            else:
                # If it *is* the very last index, then there are only three points
                # that characterize the consumption function: the artificial borrowing
                # constraint, the constraint kink, and the extrapolation point.
                mXtra = (cNrmNow[-1] - cNrmCnst[-1]) / (1.0 - self.MPCmin)
                mCrit = mNrmNow[-1] + mXtra
                cCrit = mCrit - BoroCnstArt
                mNrmNow = np.array([BoroCnstArt, mCrit, mCrit + 1.0])
                cNrmNow = np.array([0.0, cCrit, cCrit + self.MPCmin])

        # If the mNrm and cNrm grids have become too large, throw out the last
        # kink point, being sure to adjust the extrapolation.
        if mNrmNow.size > self.MaxKinks:
            mNrmNow = np.concatenate((mNrmNow[:-2], [mNrmNow[-3] + 1.0]))
            cNrmNow = np.concatenate((cNrmNow[:-2], [cNrmNow[-3] + self.MPCmin]))

        # Construct the consumption function as a linear interpolation.
        self.cFunc = LinearInterp(mNrmNow, cNrmNow)

        # Calculate the upper bound of the MPC as the slope of the bottom segment.
        self.MPCmax = (cNrmNow[1] - cNrmNow[0]) / (mNrmNow[1] - mNrmNow[0])

        # Add two attributes to enable calculation of steady state market resources.
        self.Ex_IncNext = 1.0  # Perfect foresight income of 1
        self.mNrmMinNow = mNrmNow[0]

    def solve(self):
        """
        Solves the one period perfect foresight consumption-saving problem.

        Parameters
        ----------
        None

        Returns
        -------
        solution : ConsumerSolution
            The solution to this period's problem.
        """
        self.def_utility_funcs()
        self.DiscFacEff = self.DiscFac * self.LivPrb  # Effective=pure x LivPrb
        self.make_cFunc_PF()
        self.def_value_funcs()

        solution = ConsumerSolution(
            cFunc=self.cFunc,
            vFunc=self.vFunc,
            vPfunc=self.vPfunc,
            mNrmMin=self.mNrmMinNow,
            hNrm=self.hNrmNow,
            MPCmin=self.MPCmin,
            MPCmax=self.MPCmax,
        )

        return solution


###############################################################################
###############################################################################
class ConsIndShockSetup(ConsPerfForesightSolver):
    """
    A superclass for solvers of one period consumption-saving problems with
    constant relative risk aversion utility and permanent and transitory shocks
    to income.  Has methods to set up but not solve the one period problem.

    Parameters
    ----------
    solution_next : ConsumerSolution
        The solution to next period's one period problem.
    IncShkDstn : distribution.Distribution
        A discrete approximation to the income process between the period being
        solved and the one immediately following (in solution_next).
    LivPrb : float
        Survival probability; likelihood of being alive at the beginning of
        the succeeding period.
    DiscFac : float
        Intertemporal discount factor for future utility.
    CRRA : float
        Coefficient of relative risk aversion.
    Rfree : float
        Risk free interest factor on end-of-period assets.
    PermGroFac : float
        Expected permanent income growth factor at the end of this period.
    BoroCnstArt: float or None
        Borrowing constraint for the minimum allowable assets to end the
        period with.  If it is less than the natural borrowing constraint,
        then it is irrelevant; BoroCnstArt=None indicates no artificial bor-
        rowing constraint.
    aXtraGrid: np.array
        Array of "extra" end-of-period asset values-- assets above the
        absolute minimum acceptable level.
    vFuncBool: boolean
        An indicator for whether the value function should be computed and
        included in the reported solution.
    CubicBool: boolean
        An indicator for whether the solver should use cubic or linear inter-
        polation.
    """

    def __init__(
        self,
        solution_next,
        IncShkDstn,
        LivPrb,
        DiscFac,
        CRRA,
        Rfree,
        PermGroFac,
        BoroCnstArt,
        aXtraGrid,
        vFuncBool,
        CubicBool,
    ):
        """
        Constructor for a new solver-setup for problems with income subject to
        permanent and transitory shocks.
        """
        self.solution_next = solution_next
        self.IncShkDstn = IncShkDstn
        self.LivPrb = LivPrb
        self.DiscFac = DiscFac
        self.CRRA = CRRA
        self.Rfree = Rfree
        self.PermGroFac = PermGroFac
        self.BoroCnstArt = BoroCnstArt
        self.aXtraGrid = aXtraGrid
        self.vFuncBool = vFuncBool
        self.CubicBool = CubicBool

        self.def_utility_funcs()

    def set_and_update_values(self, solution_next, IncShkDstn, LivPrb, DiscFac):
        """
        Unpacks some of the inputs (and calculates simple objects based on them),
        storing the results in self for use by other methods.  These include:
        income shocks and probabilities, next period's marginal value function
        (etc), the probability of getting the worst income shock next period,
        the patience factor, human wealth, and the bounding MPCs.

        Parameters
        ----------
        solution_next : ConsumerSolution
            The solution to next period's one period problem.
        IncShkDstn : distribution.DiscreteDistribution
            A DiscreteDistribution with a pmv
            and two point value arrays in atoms, order:
            permanent shocks, transitory shocks.
        LivPrb : float
            Survival probability; likelihood of being alive at the beginning of
            the succeeding period.
        DiscFac : float
            Intertemporal discount factor for future utility.

        Returns
        -------
        None
        """
        self.DiscFacEff = DiscFac * LivPrb  # "effective" discount factor
        self.IncShkDstn = IncShkDstn
        self.ShkPrbsNext = IncShkDstn.pmv
        self.PermShkValsNext = IncShkDstn.atoms[0]
        self.TranShkValsNext = IncShkDstn.atoms[1]
        self.PermShkMinNext = np.min(self.PermShkValsNext)
        self.TranShkMinNext = np.min(self.TranShkValsNext)
        self.vPfuncNext = solution_next.vPfunc
        self.WorstIncPrb = np.sum(
            self.ShkPrbsNext[
                (self.PermShkValsNext * self.TranShkValsNext)
                == (self.PermShkMinNext * self.TranShkMinNext)
            ]
        )

        if self.CubicBool:
            self.vPPfuncNext = solution_next.vPPfunc

        if self.vFuncBool:
            self.vFuncNext = solution_next.vFunc

        # Update the bounding MPCs and PDV of human wealth:
        self.PatFac = ((self.Rfree * self.DiscFacEff) ** (1.0 / self.CRRA)) / self.Rfree
        try:
            self.MPCminNow = 1.0 / (1.0 + self.PatFac / solution_next.MPCmin)
        except:
            self.MPCminNow = 0.0
        self.Ex_IncNext = np.dot(
            self.ShkPrbsNext, self.TranShkValsNext * self.PermShkValsNext
        )
        self.hNrmNow = (
            self.PermGroFac / self.Rfree * (self.Ex_IncNext + solution_next.hNrm)
        )
        self.MPCmaxNow = 1.0 / (
            1.0
            + (self.WorstIncPrb ** (1.0 / self.CRRA))
            * self.PatFac
            / solution_next.MPCmax
        )

        self.cFuncLimitIntercept = self.MPCminNow * self.hNrmNow
        self.cFuncLimitSlope = self.MPCminNow

    def def_BoroCnst(self, BoroCnstArt):
        """
        Defines the constrained portion of the consumption function as cFuncNowCnst,
        an attribute of self.  Uses the artificial and natural borrowing constraints.

        Parameters
        ----------
        BoroCnstArt : float or None
            Borrowing constraint for the minimum allowable assets to end the
            period with.  If it is less than the natural borrowing constraint,
            then it is irrelevant; BoroCnstArt=None indicates no artificial bor-
            rowing constraint.

        Returns
        -------
        none
        """
        # Calculate the minimum allowable value of money resources in this period
        self.BoroCnstNat = (
            (self.solution_next.mNrmMin - self.TranShkMinNext)
            * (self.PermGroFac * self.PermShkMinNext)
            / self.Rfree
        )

        # Note: need to be sure to handle BoroCnstArt==None appropriately.
        # In Py2, this would evaluate to 5.0:  np.max([None, 5.0]).
        # However in Py3, this raises a TypeError. Thus here we need to directly
        # address the situation in which BoroCnstArt == None:
        if BoroCnstArt is None:
            self.mNrmMinNow = self.BoroCnstNat
        else:
            self.mNrmMinNow = np.max([self.BoroCnstNat, BoroCnstArt])
        if self.BoroCnstNat < self.mNrmMinNow:
            self.MPCmaxEff = 1.0  # If actually constrained, MPC near limit is 1
        else:
            self.MPCmaxEff = self.MPCmaxNow

        # Define the borrowing constraint (limiting consumption function)
        self.cFuncNowCnst = LinearInterp(
            np.array([self.mNrmMinNow, self.mNrmMinNow + 1]), np.array([0.0, 1.0])
        )

    def prepare_to_solve(self):
        """
        Perform preparatory work before calculating the unconstrained consumption
        function.

        Parameters
        ----------
        none

        Returns
        -------
        none
        """
        self.set_and_update_values(
            self.solution_next, self.IncShkDstn, self.LivPrb, self.DiscFac
        )
        self.def_BoroCnst(self.BoroCnstArt)


####################################################################################################
####################################################################################################


class ConsIndShockSolverBasic(ConsIndShockSetup):
    """
    This class solves a single period of a standard consumption-saving problem,
    using linear interpolation and without the ability to calculate the value
    function.  ConsIndShockSolver inherits from this class and adds the ability
    to perform cubic interpolation and to calculate the value function.

    Note that this class does not have its own initializing method.  It initial-
    izes the same problem in the same way as ConsIndShockSetup, from which it
    inherits.
    """

    def prepare_to_calc_EndOfPrdvP(self):
        """
        Prepare to calculate end-of-period marginal value by creating an array
        of market resources that the agent could have next period, considering
        the grid of end-of-period assets and the distribution of shocks he might
        experience next period.

        Parameters
        ----------
        none

        Returns
        -------
        aNrmNow : np.array
            A 1D array of end-of-period assets; also stored as attribute of self.
        """

        # We define aNrmNow all the way from BoroCnstNat up to max(self.aXtraGrid)
        # even if BoroCnstNat < BoroCnstArt, so we can construct the consumption
        # function as the lower envelope of the (by the artificial borrowing con-
        # straint) unconstrained consumption function, and the artificially con-
        # strained consumption function.
        self.aNrmNow = np.asarray(self.aXtraGrid) + self.BoroCnstNat

        return self.aNrmNow

    def m_nrm_next(self, shocks, a_nrm, Rfree):
        """
        Computes normalized market resources of the next period
        from income shocks and current normalized market resources.

        Parameters
        ----------
        shocks: [float]
            Permanent and transitory income shock levels.
        a_nrm: float
            Normalized market assets this period

        Returns
        -------
        float
           normalized market resources in the next period
        """
        return Rfree / (self.PermGroFac * shocks["PermShk"]) * a_nrm + shocks["TranShk"]

    def calc_EndOfPrdvP(self):
        """
        Calculate end-of-period marginal value of assets at each point in aNrmNow.
        Does so by taking a weighted sum of next period marginal values across
        income shocks (in a preconstructed grid self.mNrmNext).

        Parameters
        ----------
        none

        Returns
        -------
        EndOfPrdvP : np.array
            A 1D array of end-of-period marginal value of assets
        """

        def vp_next(shocks, a_nrm, Rfree):
            return shocks["PermShk"] ** (-self.CRRA) * self.vPfuncNext(
                self.m_nrm_next(shocks, a_nrm, Rfree)
            )

        EndOfPrdvP = (
            self.DiscFacEff
            * self.Rfree
            * self.PermGroFac ** (-self.CRRA)
            * expected(vp_next, self.IncShkDstn, args=(self.aNrmNow, self.Rfree))
        )

        return EndOfPrdvP

    def get_points_for_interpolation(self, EndOfPrdvP, aNrmNow):
        """
        Finds interpolation points (c,m) for the consumption function.

        Parameters
        ----------
        EndOfPrdvP : np.array
            Array of end-of-period marginal values.
        aNrmNow : np.array
            Array of end-of-period asset values that yield the marginal values
            in EndOfPrdvP.

        Returns
        -------
        c_for_interpolation : np.array
            Consumption points for interpolation.
        m_for_interpolation : np.array
            Corresponding market resource points for interpolation.
        """
        cNrmNow = self.u.derinv(EndOfPrdvP, order=(1, 0))
        mNrmNow = cNrmNow + aNrmNow

        # Limiting consumption is zero as m approaches mNrmMin
        c_for_interpolation = np.insert(cNrmNow, 0, 0.0, axis=-1)
        m_for_interpolation = np.insert(mNrmNow, 0, self.BoroCnstNat, axis=-1)

        # Store these for calcvFunc
        self.cNrmNow = cNrmNow
        self.mNrmNow = mNrmNow

        return c_for_interpolation, m_for_interpolation

    def use_points_for_interpolation(self, cNrm, mNrm, interpolator):
        """
        Constructs a basic solution for this period, including the consumption
        function and marginal value function.

        Parameters
        ----------
        cNrm : np.array
            (Normalized) consumption points for interpolation.
        mNrm : np.array
            (Normalized) corresponding market resource points for interpolation.
        interpolator : function
            A function that constructs and returns a consumption function.

        Returns
        -------
        solution_now : ConsumerSolution
            The solution to this period's consumption-saving problem, with a
            consumption function, marginal value function, and minimum m.
        """
        # Construct the unconstrained consumption function
        cFuncNowUnc = interpolator(mNrm, cNrm)

        # Combine the constrained and unconstrained functions into the true consumption function
        # LowerEnvelope should only be used when BoroCnstArt is true
        cFuncNow = LowerEnvelope(cFuncNowUnc, self.cFuncNowCnst, nan_bool=False)

        # Make the marginal value function and the marginal marginal value function
        vPfuncNow = MargValueFuncCRRA(cFuncNow, self.CRRA)

        # Pack up the solution and return it
        solution_now = ConsumerSolution(
            cFunc=cFuncNow, vPfunc=vPfuncNow, mNrmMin=self.mNrmMinNow
        )

        return solution_now

    def make_basic_solution(self, EndOfPrdvP, aNrm, interpolator):
        """
        Given end of period assets and end of period marginal value, construct
        the basic solution for this period.

        Parameters
        ----------
        EndOfPrdvP : np.array
            Array of end-of-period marginal values.
        aNrm : np.array
            Array of end-of-period asset values that yield the marginal values
            in EndOfPrdvP.

        interpolator : function
            A function that constructs and returns a consumption function.

        Returns
        -------
        solution_now : ConsumerSolution
            The solution to this period's consumption-saving problem, with a
            consumption function, marginal value function, and minimum m.
        """
        cNrm, mNrm = self.get_points_for_interpolation(EndOfPrdvP, aNrm)
        solution_now = self.use_points_for_interpolation(cNrm, mNrm, interpolator)

        return solution_now

    def add_MPC_and_human_wealth(self, solution):
        """
        Take a solution and add human wealth and the bounding MPCs to it.

        Parameters
        ----------
        solution : ConsumerSolution
            The solution to this period's consumption-saving problem.

        Returns:
        ----------
        solution : ConsumerSolution
            The solution to this period's consumption-saving problem, but now
            with human wealth and the bounding MPCs.
        """
        solution.hNrm = self.hNrmNow
        solution.MPCmin = self.MPCminNow
        solution.MPCmax = self.MPCmaxEff
        return solution

    def make_linear_cFunc(self, mNrm, cNrm):
        """
        Makes a linear interpolation to represent the (unconstrained) consumption function.

        Parameters
        ----------
        mNrm : np.array
            Corresponding market resource points for interpolation.
        cNrm : np.array
            Consumption points for interpolation.

        Returns
        -------
        cFuncUnc : LinearInterp
            The unconstrained consumption function for this period.
        """
        cFuncUnc = LinearInterp(
            mNrm, cNrm, self.cFuncLimitIntercept, self.cFuncLimitSlope
        )
        return cFuncUnc

    def solve(self):
        """
        Solves a one period consumption saving problem with risky income.

        Parameters
        ----------
        None

        Returns
        -------
        solution : ConsumerSolution
            The solution to the one period problem.
        """
        aNrmNow = self.prepare_to_calc_EndOfPrdvP()
        EndOfPrdvP = self.calc_EndOfPrdvP()
        solution = self.make_basic_solution(EndOfPrdvP, aNrmNow, self.make_linear_cFunc)
        solution = self.add_MPC_and_human_wealth(solution)

        return solution


###############################################################################
###############################################################################


class ConsIndShockSolver(ConsIndShockSolverBasic):
    """
    This class solves a single period of a standard consumption-saving problem.
    It inherits from ConsIndShockSolverBasic, adding the ability to perform cubic
    interpolation and to calculate the value function.
    """

    def make_cubic_cFunc(self, mNrm, cNrm):
        """
        Makes a cubic spline interpolation of the unconstrained consumption
        function for this period.

        Parameters
        ----------
        mNrm : np.array
            Corresponding market resource points for interpolation.
        cNrm : np.array
            Consumption points for interpolation.

        Returns
        -------
        cFuncUnc : CubicInterp
            The unconstrained consumption function for this period.
        """

        def vpp_next(shocks, a_nrm, Rfree):
            return shocks["PermShk"] ** (-self.CRRA - 1.0) * self.vPPfuncNext(
                self.m_nrm_next(shocks, a_nrm, Rfree)
            )

        EndOfPrdvPP = (
            self.DiscFacEff
            * self.Rfree
            * self.Rfree
            * self.PermGroFac ** (-self.CRRA - 1.0)
            * expected(vpp_next, self.IncShkDstn, args=(self.aNrmNow, self.Rfree))
        )
        dcda = EndOfPrdvPP / self.u.der(np.array(cNrm[1:]), order=2)
        MPC = dcda / (dcda + 1.0)
        MPC = np.insert(MPC, 0, self.MPCmaxNow)

        cFuncNowUnc = CubicInterp(
            mNrm, cNrm, MPC, self.MPCminNow * self.hNrmNow, self.MPCminNow
        )
        return cFuncNowUnc

    def make_EndOfPrdvFunc(self, EndOfPrdvP):
        """
        Construct the end-of-period value function for this period, storing it
        as an attribute of self for use by other methods.

        Parameters
        ----------
        EndOfPrdvP : np.array
            Array of end-of-period marginal value of assets corresponding to the
            asset values in self.aNrmNow.

        Returns
        -------
        none
        """

        def v_lvl_next(shocks, a_nrm, Rfree):
            return (
                shocks["PermShk"] ** (1.0 - self.CRRA)
                * self.PermGroFac ** (1.0 - self.CRRA)
            ) * self.vFuncNext(self.m_nrm_next(shocks, a_nrm, Rfree))

        EndOfPrdv = self.DiscFacEff * expected(
            v_lvl_next, self.IncShkDstn, args=(self.aNrmNow, self.Rfree)
        )
        EndOfPrdvNvrs = self.u.inv(
            EndOfPrdv
        )  # value transformed through inverse utility
        EndOfPrdvNvrsP = EndOfPrdvP * self.u.derinv(EndOfPrdv, order=(0, 1))
        EndOfPrdvNvrs = np.insert(EndOfPrdvNvrs, 0, 0.0)
        EndOfPrdvNvrsP = np.insert(
            EndOfPrdvNvrsP, 0, EndOfPrdvNvrsP[0]
        )  # This is a very good approximation, vNvrsPP = 0 at the asset minimum
        aNrm_temp = np.insert(self.aNrmNow, 0, self.BoroCnstNat)
        EndOfPrdvNvrsFunc = CubicInterp(aNrm_temp, EndOfPrdvNvrs, EndOfPrdvNvrsP)
        self.EndOfPrdvFunc = ValueFuncCRRA(EndOfPrdvNvrsFunc, self.CRRA)

    def add_vFunc(self, solution, EndOfPrdvP):
        """
        Creates the value function for this period and adds it to the solution.

        Parameters
        ----------
        solution : ConsumerSolution
            The solution to this single period problem, likely including the
            consumption function, marginal value function, etc.
        EndOfPrdvP : np.array
            Array of end-of-period marginal value of assets corresponding to the
            asset values in self.aNrmNow.

        Returns
        -------
        solution : ConsumerSolution
            The single period solution passed as an input, but now with the
            value function (defined over market resources m) as an attribute.
        """
        self.make_EndOfPrdvFunc(EndOfPrdvP)
        solution.vFunc = self.make_vFunc(solution)
        return solution

    def make_vFunc(self, solution):
        """
        Creates the value function for this period, defined over market resources m.
        self must have the attribute EndOfPrdvFunc in order to execute.

        Parameters
        ----------
        solution : ConsumerSolution
            The solution to this single period problem, which must include the
            consumption function.

        Returns
        -------
        vFuncNow : ValueFuncCRRA
            A representation of the value function for this period, defined over
            normalized market resources m: v = vFuncNow(m).
        """
        # Compute expected value and marginal value on a grid of market resources
        mNrm_temp = self.mNrmMinNow + self.aXtraGrid
        cNrmNow = solution.cFunc(mNrm_temp)
        aNrmNow = mNrm_temp - cNrmNow
        vNrmNow = self.u(cNrmNow) + self.EndOfPrdvFunc(aNrmNow)
        vPnow = self.u.der(cNrmNow)

        # Construct the beginning-of-period value function
        # value transformed through inverse utility
        vNvrs = self.u.inv(vNrmNow)
        vNvrsP = vPnow * self.u.derinv(vNrmNow, order=(0, 1))
        mNrm_temp = np.insert(mNrm_temp, 0, self.mNrmMinNow)
        vNvrs = np.insert(vNvrs, 0, 0.0)
        vNvrsP = np.insert(
            vNvrsP, 0, self.MPCmaxEff ** (-self.CRRA / (1.0 - self.CRRA))
        )
        MPCminNvrs = self.MPCminNow ** (-self.CRRA / (1.0 - self.CRRA))
        vNvrsFuncNow = CubicInterp(
            mNrm_temp, vNvrs, vNvrsP, MPCminNvrs * self.hNrmNow, MPCminNvrs
        )
        vFuncNow = ValueFuncCRRA(vNvrsFuncNow, self.CRRA)
        return vFuncNow

    def add_vPPfunc(self, solution):
        """
        Adds the marginal marginal value function to an existing solution, so
        that the next solver can evaluate vPP and thus use cubic interpolation.

        Parameters
        ----------
        solution : ConsumerSolution
            The solution to this single period problem, which must include the
            consumption function.

        Returns
        -------
        solution : ConsumerSolution
            The same solution passed as input, but with the marginal marginal
            value function for this period added as the attribute vPPfunc.
        """
        vPPfuncNow = MargMargValueFuncCRRA(solution.cFunc, self.CRRA)
        solution.vPPfunc = vPPfuncNow
        return solution

    def solve(self):
        """
        Solves the single period consumption-saving problem using the method of
        endogenous gridpoints.  Solution includes a consumption function cFunc
        (using cubic or linear splines), a marginal value function vPfunc, a min-
        imum acceptable level of normalized market resources mNrmMin, normalized
        human wealth hNrm, and bounding MPCs MPCmin and MPCmax.  It might also
        have a value function vFunc and marginal marginal value function vPPfunc.

        Parameters
        ----------
        none

        Returns
        -------
        solution : ConsumerSolution
            The solution to the single period consumption-saving problem.
        """
        # Make arrays of end-of-period assets and end-of-period marginal value
        aNrm = self.prepare_to_calc_EndOfPrdvP()
        EndOfPrdvP = self.calc_EndOfPrdvP()

        # Construct a basic solution for this period
        if self.CubicBool:
            solution = self.make_basic_solution(
                EndOfPrdvP, aNrm, interpolator=self.make_cubic_cFunc
            )
        else:
            solution = self.make_basic_solution(
                EndOfPrdvP, aNrm, interpolator=self.make_linear_cFunc
            )

        solution = self.add_MPC_and_human_wealth(solution)  # add a few things

        # Add the value function if requested, as well as the marginal marginal
        # value function if cubic splines were used (to prepare for next period)
        if self.vFuncBool:
            solution = self.add_vFunc(solution, EndOfPrdvP)
        if self.CubicBool:
            solution = self.add_vPPfunc(solution)
        return solution


####################################################################################################
####################################################################################################


class ConsKinkedRsolver(ConsIndShockSolver):
    """
    A class to solve a single period consumption-saving problem where the interest
    rate on debt differs from the interest rate on savings.  Inherits from
    ConsIndShockSolver, with nearly identical inputs and outputs.  The key diff-
    erence is that Rfree is replaced by Rsave (a>0) and Rboro (a<0).  The solver
    can handle Rboro == Rsave, which makes it identical to ConsIndShocksolver, but
    it terminates immediately if Rboro < Rsave, as this has a different solution.

    Parameters
    ----------
    solution_next : ConsumerSolution
        The solution to next period's one period problem.
    IncShkDstn : distribution.Distribution
        A discrete
        approximation to the income process between the period being solved
        and the one immediately following (in solution_next).
    LivPrb : float
        Survival probability; likelihood of being alive at the beginning of
        the succeeding period.
    DiscFac : float
        Intertemporal discount factor for future utility.
    CRRA : float
        Coefficient of relative risk aversion.
    Rboro: float
        Interest factor on assets between this period and the succeeding
        period when assets are negative.
    Rsave: float
        Interest factor on assets between this period and the succeeding
        period when assets are positive.
    PermGroFac : float
        Expected permanent income growth factor at the end of this period.
    BoroCnstArt: float or None
        Borrowing constraint for the minimum allowable assets to end the
        period with.  If it is less than the natural borrowing constraint,
        then it is irrelevant; BoroCnstArt=None indicates no artificial bor-
        rowing constraint.
    aXtraGrid: np.array
        Array of "extra" end-of-period asset values-- assets above the
        absolute minimum acceptable level.
    vFuncBool: boolean
        An indicator for whether the value function should be computed and
        included in the reported solution.
    CubicBool: boolean
        An indicator for whether the solver should use cubic or linear inter-
        polation.
    """

    def __init__(
        self,
        solution_next,
        IncShkDstn,
        LivPrb,
        DiscFac,
        CRRA,
        Rboro,
        Rsave,
        PermGroFac,
        BoroCnstArt,
        aXtraGrid,
        vFuncBool,
        CubicBool,
    ):
        assert (
            Rboro >= Rsave
        ), "Interest factor on debt less than interest factor on savings!"

        # Initialize the solver.  Most of the steps are exactly the same as in
        # the non-kinked-R basic case, so start with that.
        ConsIndShockSolver.__init__(
            self,
            solution_next,
            IncShkDstn,
            LivPrb,
            DiscFac,
            CRRA,
            Rboro,
            PermGroFac,
            BoroCnstArt,
            aXtraGrid,
            vFuncBool,
            CubicBool,
        )

        # Assign the interest rates as class attributes, to use them later.
        self.Rboro = Rboro
        self.Rsave = Rsave

    def make_cubic_cFunc(self, mNrm, cNrm):
        """
        Makes a cubic spline interpolation that contains the kink of the unconstrained
        consumption function for this period.

        Parameters
        ----------
        mNrm : np.array
            Corresponding market resource points for interpolation.
        cNrm : np.array
            Consumption points for interpolation.

        Returns
        -------
        cFuncUnc : CubicInterp
            The unconstrained consumption function for this period.
        """
        # Call the make_cubic_cFunc from ConsIndShockSolver.
        cFuncNowUncKink = super().make_cubic_cFunc(mNrm, cNrm)

        # Change the coeffients at the kinked points.
        cFuncNowUncKink.coeffs[self.i_kink + 1] = [
            cNrm[self.i_kink],
            mNrm[self.i_kink + 1] - mNrm[self.i_kink],
            0,
            0,
        ]

        return cFuncNowUncKink

    def prepare_to_calc_EndOfPrdvP(self):
        """
        Prepare to calculate end-of-period marginal value by creating an array
        of market resources that the agent could have next period, considering
        the grid of end-of-period assets and the distribution of shocks he might
        experience next period.  This differs from the baseline case because
        different savings choices yield different interest rates.

        Parameters
        ----------
        none

        Returns
        -------
        aNrmNow : np.array
            A 1D array of end-of-period assets; also stored as attribute of self.
        """
        KinkBool = (
            self.Rboro > self.Rsave
        )  # Boolean indicating that there is actually a kink.
        # When Rboro == Rsave, this method acts just like it did in IndShock.
        # When Rboro < Rsave, the solver would have terminated when it was called.

        # Make a grid of end-of-period assets, including *two* copies of a=0
        if KinkBool:
            aNrmNow = np.sort(
                np.hstack(
                    (np.asarray(self.aXtraGrid) + self.mNrmMinNow, np.array([0.0, 0.0]))
                )
            )
        else:
            aNrmNow = np.asarray(self.aXtraGrid) + self.mNrmMinNow
        aXtraCount = aNrmNow.size

        # Make tiled versions of the assets grid and income shocks
        ShkCount = self.TranShkValsNext.size
        aNrm_temp = np.tile(aNrmNow, (ShkCount, 1))
        PermShkVals_temp = (np.tile(self.PermShkValsNext, (aXtraCount, 1))).transpose()
        TranShkVals_temp = (np.tile(self.TranShkValsNext, (aXtraCount, 1))).transpose()
        ShkPrbs_temp = (np.tile(self.ShkPrbsNext, (aXtraCount, 1))).transpose()

        # Make a 1D array of the interest factor at each asset gridpoint
        Rfree_vec = self.Rsave * np.ones(aXtraCount)
        if KinkBool:
            self.i_kink = (
                np.sum(aNrmNow <= 0) - 1
            )  # Save the index of the kink point as an attribute
            Rfree_vec[0 : self.i_kink] = self.Rboro
        self.Rfree = Rfree_vec
        Rfree_temp = np.tile(Rfree_vec, (ShkCount, 1))

        # Make an array of market resources that we could have next period,
        # considering the grid of assets and the income shocks that could occur
        mNrmNext = (
            Rfree_temp / (self.PermGroFac * PermShkVals_temp) * aNrm_temp
            + TranShkVals_temp
        )

        # Recalculate the minimum MPC and human wealth using the interest factor on saving.
        # This overwrites values from set_and_update_values, which were based on Rboro instead.
        if KinkBool:
            PatFacTop = (
                (self.Rsave * self.DiscFacEff) ** (1.0 / self.CRRA)
            ) / self.Rsave
            self.MPCminNow = 1.0 / (1.0 + PatFacTop / self.solution_next.MPCmin)
            self.hNrmNow = (
                self.PermGroFac
                / self.Rsave
                * (
                    np.dot(
                        self.ShkPrbsNext, self.TranShkValsNext * self.PermShkValsNext
                    )
                    + self.solution_next.hNrm
                )
            )

        # Store some of the constructed arrays for later use and return the assets grid
        self.PermShkVals_temp = PermShkVals_temp
        self.ShkPrbs_temp = ShkPrbs_temp
        self.mNrmNext = mNrmNext
        self.aNrmNow = aNrmNow
        return aNrmNow


##############################################################################


class ConsPortfolioSolver(MetricObject):
    """
    Define an object-oriented one period solver.
    Solve the one period problem for a portfolio-choice consumer.
    This solver is used when the income and risky return shocks
    are independent and the allowed optimal share is continuous.

    Parameters
    ----------
    solution_next : PortfolioSolution
        Solution to next period's problem.
    ShockDstn : [np.array]
        List with four arrays: discrete probabilities, permanent income shocks,
        transitory income shocks, and risky returns.  This is only used if the
        input IndepDstnBool is False, indicating that income and return distributions
        can't be assumed to be independent.
    IncShkDstn : distribution.Distribution
        Discrete distribution of permanent income shocks
        and transitory income shocks.  This is only used if the input IndepDsntBool
        is True, indicating that income and return distributions are independent.
    RiskyDstn : [np.array]
        List with two arrays: discrete probabilities and risky asset returns. This
        is only used if the input IndepDstnBool is True, indicating that income
        and return distributions are independent.
    LivPrb : float
        Survival probability; likelihood of being alive at the beginning of
        the succeeding period.
    DiscFac : float
        Intertemporal discount factor for future utility.
    CRRA : float
        Coefficient of relative risk aversion.
    Rfree : float
        Risk free interest factor on end-of-period assets.
    PermGroFac : float
        Expected permanent income growth factor at the end of this period.
    BoroCnstArt: float or None
        Borrowing constraint for the minimum allowable assets to end the
        period with.  In this model, it is *required* to be zero.
    aXtraGrid: np.array
        Array of "extra" end-of-period asset values-- assets above the
        absolute minimum acceptable level.
    ShareGrid : np.array
        Array of risky portfolio shares on which to define the interpolation
        of the consumption function when Share is fixed.
    vFuncBool: boolean
        An indicator for whether the value function should be computed and
        included in the reported solution.
    AdjustPrb : float
        Probability that the agent will be able to update his portfolio share.
    DiscreteShareBool : bool
        Indicator for whether risky portfolio share should be optimized on the
        continuous [0,1] interval using the FOC (False), or instead only selected
        from the discrete set of values in ShareGrid (True).  If True, then
        vFuncBool must also be True.
    ShareLimit : float
        Limiting lower bound of risky portfolio share as mNrm approaches infinity.
    IndepDstnBool : bool
        Indicator for whether the income and risky return distributions are in-
        dependent of each other, which can speed up the expectations step.
    """

    def __init__(
        self,
        solution_next,
        ShockDstn,
        IncShkDstn,
        RiskyDstn,
        LivPrb,
        DiscFac,
        CRRA,
        Rfree,
        PermGroFac,
        BoroCnstArt,
        aXtraGrid,
        ShareGrid,
        vFuncBool,
        AdjustPrb,
        DiscreteShareBool,
        ShareLimit,
        IndepDstnBool,
    ):
        """
        Constructor for portfolio choice problem solver.
        """

        self.solution_next = solution_next
        self.ShockDstn = ShockDstn
        self.IncShkDstn = IncShkDstn
        self.RiskyDstn = RiskyDstn
        self.LivPrb = LivPrb
        self.DiscFac = DiscFac
        self.CRRA = CRRA
        self.Rfree = Rfree
        self.PermGroFac = PermGroFac
        self.BoroCnstArt = BoroCnstArt
        self.aXtraGrid = aXtraGrid
        self.ShareGrid = ShareGrid
        self.vFuncBool = vFuncBool
        self.AdjustPrb = AdjustPrb
        self.DiscreteShareBool = DiscreteShareBool
        self.ShareLimit = ShareLimit
        self.IndepDstnBool = IndepDstnBool

        # Make sure the individual is liquidity constrained.  Allowing a consumer to
        # borrow *and* invest in an asset with unbounded (negative) returns is a bad mix.
        if BoroCnstArt != 0.0:
            raise ValueError("PortfolioConsumerType must have BoroCnstArt=0.0!")

        # Make sure that if risky portfolio share is optimized only discretely, then
        # the value function is also constructed (else this task would be impossible).
        if DiscreteShareBool and (not vFuncBool):
            raise ValueError(
                "PortfolioConsumerType requires vFuncBool to be True when DiscreteShareBool is True!"
            )

        self.def_utility_funcs()

    def def_utility_funcs(self):
        """
        Define temporary functions for utility and its derivative and inverse
        """

        self.u = lambda x: utility(x, self.CRRA)
        self.uP = lambda x: utilityP(x, self.CRRA)
        self.uPinv = lambda x: utilityP_inv(x, self.CRRA)
        self.uinv = lambda x: utility_inv(x, self.CRRA)
        self.uinvP = lambda x: utility_invP(x, self.CRRA)

    def set_and_update_values(self):
        """
        Unpacks some of the inputs (and calculates simple objects based on them),
        storing the results in self for use by other methods.
        """

        # Unpack next period's solution
        self.vPfuncAdj_next = self.solution_next.vPfuncAdj
        self.dvdmFuncFxd_next = self.solution_next.dvdmFuncFxd
        self.dvdsFuncFxd_next = self.solution_next.dvdsFuncFxd
        self.vFuncAdj_next = self.solution_next.vFuncAdj
        self.vFuncFxd_next = self.solution_next.vFuncFxd

        # Unpack the shock distribution
        TranShks_next = self.IncShkDstn.atoms[1]

        # Flag for whether the natural borrowing constraint is zero
        self.zero_bound = np.min(TranShks_next) == 0.0

    def prepare_to_solve(self):
        """
        Perform preparatory work.
        """

        self.set_and_update_values()

    def prepare_to_calc_EndOfPrdvP(self):
        """
        Prepare to calculate end-of-period marginal values by creating an array
        of market resources that the agent could have next period, considering
        the grid of end-of-period assets and the distribution of shocks he might
        experience next period.
        """

        # Unpack the shock distribution
        Risky_next = self.RiskyDstn.atoms
        RiskyMax = np.max(Risky_next)
        RiskyMin = np.min(Risky_next)

        # bNrm represents R*a, balances after asset return shocks but before income.
        # This just uses the highest risky return as a rough shifter for the aXtraGrid.
        if self.zero_bound:
            self.aNrmGrid = self.aXtraGrid
            self.bNrmGrid = np.insert(
                RiskyMax * self.aXtraGrid, 0, RiskyMin * self.aXtraGrid[0]
            )
        else:
            # Add an asset point at exactly zero
            self.aNrmGrid = np.insert(self.aXtraGrid, 0, 0.0)
            self.bNrmGrid = RiskyMax * np.insert(self.aXtraGrid, 0, 0.0)

        # Get grid and shock sizes, for easier indexing
        self.aNrmCount = self.aNrmGrid.size
        self.ShareCount = self.ShareGrid.size

        # Make tiled arrays to calculate future realizations of mNrm and Share when integrating over IncShkDstn
        self.bNrmNext, self.ShareNext = np.meshgrid(
            self.bNrmGrid, self.ShareGrid, indexing="ij"
        )

    def m_nrm_next(self, shocks, b_nrm_next):
        """
        Calculate future realizations of market resources
        """

        return b_nrm_next / (shocks["PermShk"] * self.PermGroFac) + shocks["TranShk"]

    def calc_EndOfPrdvP(self):
        """
        Calculate end-of-period marginal value of assets and shares at each point
        in aNrm and ShareGrid. Does so by taking expectation of next period marginal
        values across income and risky return shocks.
        """

        def dvdb_dist(shocks, b_nrm, Share_next):
            """
            Evaluate realizations of marginal value of market resources next period
            """

            mNrm_next = self.m_nrm_next(shocks, b_nrm)

            dvdmAdj_next = self.vPfuncAdj_next(mNrm_next)
            if self.AdjustPrb < 1.0:
                # Expand to the same dimensions as mNrm
                Share_next_expanded = Share_next + np.zeros_like(mNrm_next)
                dvdmFxd_next = self.dvdmFuncFxd_next(mNrm_next, Share_next_expanded)
                # Combine by adjustment probability
                dvdm_next = (
                    self.AdjustPrb * dvdmAdj_next
                    + (1.0 - self.AdjustPrb) * dvdmFxd_next
                )
            else:  # Don't bother evaluating if there's no chance that portfolio share is fixed
                dvdm_next = dvdmAdj_next

            return (shocks["PermShk"] * self.PermGroFac) ** (-self.CRRA) * dvdm_next

        def dvds_dist(shocks, b_nrm, Share_next):
            """
            Evaluate realizations of marginal value of risky share next period
            """

            mNrm_next = self.m_nrm_next(shocks, b_nrm)
            # No marginal value of Share if it's a free choice!
            dvdsAdj_next = np.zeros_like(mNrm_next)
            if self.AdjustPrb < 1.0:
                # Expand to the same dimensions as mNrm
                Share_next_expanded = Share_next + np.zeros_like(mNrm_next)
                dvdsFxd_next = self.dvdsFuncFxd_next(mNrm_next, Share_next_expanded)
                # Combine by adjustment probability
                dvds_next = (
                    self.AdjustPrb * dvdsAdj_next
                    + (1.0 - self.AdjustPrb) * dvdsFxd_next
                )
            else:  # Don't bother evaluating if there's no chance that portfolio share is fixed
                dvds_next = dvdsAdj_next

            return (shocks["PermShk"] * self.PermGroFac) ** (
                1.0 - self.CRRA
            ) * dvds_next

        # Calculate intermediate marginal value of bank balances by taking expectations over income shocks
        dvdb_intermed = self.IncShkDstn.expected(
            dvdb_dist, self.bNrmNext, self.ShareNext
        )

        dvdbNvrs_intermed = self.uPinv(dvdb_intermed)
        dvdbNvrsFunc_intermed = BilinearInterp(
            dvdbNvrs_intermed, self.bNrmGrid, self.ShareGrid
        )
        dvdbFunc_intermed = MargValueFuncCRRA(dvdbNvrsFunc_intermed, self.CRRA)

        # Calculate intermediate marginal value of risky portfolio share by taking expectations
        dvds_intermed = self.IncShkDstn.expected(
            dvds_dist, self.bNrmNext, self.ShareNext
        )

        dvdsFunc_intermed = BilinearInterp(dvds_intermed, self.bNrmGrid, self.ShareGrid)

        # Make tiled arrays to calculate future realizations of bNrm and Share when integrating over RiskyDstn
        self.aNrm_tiled, self.ShareNext = np.meshgrid(
            self.aNrmGrid, self.ShareGrid, indexing="ij"
        )

        # Evaluate realizations of value and marginal value after asset returns are realized

        def EndOfPrddvda_dist(shock, a_nrm, Share_next):
            # Calculate future realizations of bank balances bNrm
            Rxs = shock - self.Rfree
            Rport = self.Rfree + Share_next * Rxs
            b_nrm_next = Rport * a_nrm

            # Ensure shape concordance
            Share_next_rep = Share_next + np.zeros_like(b_nrm_next)

            return Rport * dvdbFunc_intermed(b_nrm_next, Share_next_rep)

        def EndOfPrddvds_dist(shock, a_nrm, Share_next):
            # Calculate future realizations of bank balances bNrm
            Rxs = shock - self.Rfree
            Rport = self.Rfree + Share_next * Rxs
            b_nrm_next = Rport * a_nrm

            # Make the shares match the dimension of b, so that it can be vectorized
            Share_next_expand = Share_next + np.zeros_like(b_nrm_next)

            return Rxs * a_nrm * dvdbFunc_intermed(
                b_nrm_next, Share_next_expand
            ) + dvdsFunc_intermed(b_nrm_next, Share_next_expand)

        # Calculate end-of-period marginal value of assets by taking expectations
        self.EndOfPrddvda = (
            self.DiscFac
            * self.LivPrb
            * self.RiskyDstn.expected(
                EndOfPrddvda_dist, self.aNrm_tiled, self.ShareNext
            )
        )

        self.EndOfPrddvdaNvrs = self.uPinv(self.EndOfPrddvda)

        # Calculate end-of-period marginal value of risky portfolio share by taking expectations
        self.EndOfPrddvds = (
            self.DiscFac
            * self.LivPrb
            * self.RiskyDstn.expected(
                EndOfPrddvds_dist, self.aNrm_tiled, self.ShareNext
            )
        )

    def optimize_share(self):
        """
        Optimization of Share on continuous interval [0,1]
        """

        FOC_s = self.EndOfPrddvds

        # For each value of aNrm, find the value of Share such that FOC-Share == 0.
        crossing = np.logical_and(FOC_s[:, 1:] <= 0.0, FOC_s[:, :-1] >= 0.0)
        share_idx = np.argmax(crossing, axis=1)
        a_idx = np.arange(self.aNrmCount)
        bot_s = self.ShareGrid[share_idx]
        top_s = self.ShareGrid[share_idx + 1]
        bot_f = FOC_s[a_idx, share_idx]
        top_f = FOC_s[a_idx, share_idx + 1]
        bot_c = self.EndOfPrddvdaNvrs[a_idx, share_idx]
        top_c = self.EndOfPrddvdaNvrs[a_idx, share_idx + 1]
        alpha = 1.0 - top_f / (top_f - bot_f)

        self.Share_now = (1.0 - alpha) * bot_s + alpha * top_s
        self.cNrmAdj_now = (1.0 - alpha) * bot_c + alpha * top_c

        # If agent wants to put more than 100% into risky asset, he is constrained
        constrained_top = FOC_s[:, -1] > 0.0
        # Likewise if he wants to put less than 0% into risky asset
        constrained_bot = FOC_s[:, 0] < 0.0

        # For values of aNrm at which the agent wants to put
        # more than 100% into risky asset, constrain them
        self.Share_now[constrained_top] = 1.0
        self.Share_now[constrained_bot] = 0.0

        # Get consumption when share-constrained
        self.cNrmAdj_now[constrained_top] = self.EndOfPrddvdaNvrs[constrained_top, -1]
        self.cNrmAdj_now[constrained_bot] = self.EndOfPrddvdaNvrs[constrained_bot, 0]

        if not self.zero_bound:
            # aNrm=0, so there's no way to "optimize" the portfolio
            self.Share_now[0] = 1.0
            # Consumption when aNrm=0 does not depend on Share
            self.cNrmAdj_now[0] = self.EndOfPrddvdaNvrs[0, -1]

    def make_basic_solution(self):
        """
        Given end of period assets and end of period marginal values, construct
        the basic solution for this period.
        """

        # Calculate the endogenous mNrm gridpoints when the agent adjusts his portfolio
        self.mNrmAdj_now = self.aNrmGrid + self.cNrmAdj_now

        # Construct the consumption function when the agent can adjust
        cNrmAdj_now = np.insert(self.cNrmAdj_now, 0, 0.0)
        self.cFuncAdj_now = LinearInterp(
            np.insert(self.mNrmAdj_now, 0, 0.0), cNrmAdj_now
        )

        # Construct the marginal value (of mNrm) function when the agent can adjust
        self.vPfuncAdj_now = MargValueFuncCRRA(self.cFuncAdj_now, self.CRRA)

        # Construct the consumption function when the agent *can't* adjust the risky share, as well
        # as the marginal value of Share function
        cFuncFxd_by_Share = []
        dvdsFuncFxd_by_Share = []
        for j in range(self.ShareCount):
            cNrmFxd_temp = self.EndOfPrddvdaNvrs[:, j]
            mNrmFxd_temp = self.aNrmGrid + cNrmFxd_temp
            cFuncFxd_by_Share.append(
                LinearInterp(
                    np.insert(mNrmFxd_temp, 0, 0.0), np.insert(cNrmFxd_temp, 0, 0.0)
                )
            )
            dvdsFuncFxd_by_Share.append(
                LinearInterp(
                    np.insert(mNrmFxd_temp, 0, 0.0),
                    np.insert(self.EndOfPrddvds[:, j], 0, self.EndOfPrddvds[0, j]),
                )
            )
        self.cFuncFxd_now = LinearInterpOnInterp1D(cFuncFxd_by_Share, self.ShareGrid)
        self.dvdsFuncFxd_now = LinearInterpOnInterp1D(
            dvdsFuncFxd_by_Share, self.ShareGrid
        )

        # The share function when the agent can't adjust his portfolio is trivial
        self.ShareFuncFxd_now = IdentityFunction(i_dim=1, n_dims=2)

        # Construct the marginal value of mNrm function when the agent can't adjust his share
        self.dvdmFuncFxd_now = MargValueFuncCRRA(self.cFuncFxd_now, self.CRRA)

    def make_ShareFuncAdj(self):
        """
        Construct the risky share function when the agent can adjust
        """

        if self.zero_bound:
            Share_lower_bound = self.ShareLimit
        else:
            Share_lower_bound = 1.0
        Share_now = np.insert(self.Share_now, 0, Share_lower_bound)
        self.ShareFuncAdj_now = LinearInterp(
            np.insert(self.mNrmAdj_now, 0, 0.0),
            Share_now,
            intercept_limit=self.ShareLimit,
            slope_limit=0.0,
        )

    def add_save_points(self):
        # This is a point at which (a,c,share) have consistent length. Take the
        # snapshot for storing the grid and values in the solution.
        self.save_points = {
            "a": deepcopy(self.aNrmGrid),
            "eop_dvda_adj": self.uP(self.cNrmAdj_now),
            "share_adj": deepcopy(self.Share_now),
            "share_grid": deepcopy(self.ShareGrid),
            "eop_dvda_fxd": self.uP(self.EndOfPrddvda),
            "eop_dvds_fxd": self.EndOfPrddvds,
        }

    def add_vFunc(self):
        """
        Creates the value function for this period and adds it to the solution.
        """

        self.make_EndOfPrdvFunc()
        self.make_vFunc()

    def make_EndOfPrdvFunc(self):
        """
        Construct the end-of-period value function for this period, storing it
        as an attribute of self for use by other methods.
        """

        def v_intermed_dist(shocks, b_nrm, Share_next):
            mNrm_next = self.m_nrm_next(shocks, b_nrm)

            vAdj_next = self.vFuncAdj_next(mNrm_next)
            if self.AdjustPrb < 1.0:
                vFxd_next = self.vFuncFxd_next(mNrm_next, Share_next)
                # Combine by adjustment probability
                v_next = self.AdjustPrb * vAdj_next + (1.0 - self.AdjustPrb) * vFxd_next
            else:  # Don't bother evaluating if there's no chance that portfolio share is fixed
                v_next = vAdj_next

            return (shocks["PermShk"] * self.PermGroFac) ** (1.0 - self.CRRA) * v_next

        # Calculate intermediate value by taking expectations over income shocks
        v_intermed = self.IncShkDstn.expected(
            v_intermed_dist, self.bNrmNext, self.ShareNext
        )

        vNvrs_intermed = self.uinv(v_intermed)
        vNvrsFunc_intermed = BilinearInterp(
            vNvrs_intermed, self.bNrmGrid, self.ShareGrid
        )
        vFunc_intermed = ValueFuncCRRA(vNvrsFunc_intermed, self.CRRA)

        def EndOfPrdv_dist(shock, a_nrm, Share_next):
            # Calculate future realizations of bank balances bNrm
            Rxs = shock - self.Rfree
            Rport = self.Rfree + Share_next * Rxs
            b_nrm_next = Rport * a_nrm

            # Make an extended share_next of the same dimension as b_nrm so
            # that the function can be vectorized
            Share_next_extended = Share_next + np.zeros_like(b_nrm_next)

            return vFunc_intermed(b_nrm_next, Share_next_extended)

        # Calculate end-of-period value by taking expectations
        self.EndOfPrdv = (
            self.DiscFac
            * self.LivPrb
            * self.RiskyDstn.expected(EndOfPrdv_dist, self.aNrm_tiled, self.ShareNext)
        )

        self.EndOfPrdvNvrs = self.uinv(self.EndOfPrdv)

    def make_vFunc(self):
        """
        Creates the value functions for this period, defined over market
        resources m when agent can adjust his portfolio, and over market
        resources and fixed share when agent can not adjust his portfolio.
        self must have the attribute EndOfPrdvFunc in order to execute.
        """

        # First, make an end-of-period value function over aNrm and Share
        EndOfPrdvNvrsFunc = BilinearInterp(
            self.EndOfPrdvNvrs, self.aNrmGrid, self.ShareGrid
        )
        EndOfPrdvFunc = ValueFuncCRRA(EndOfPrdvNvrsFunc, self.CRRA)

        # Construct the value function when the agent can adjust his portfolio
        mNrm_temp = self.aXtraGrid  # Just use aXtraGrid as our grid of mNrm values
        cNrm_temp = self.cFuncAdj_now(mNrm_temp)
        aNrm_temp = mNrm_temp - cNrm_temp
        Share_temp = self.ShareFuncAdj_now(mNrm_temp)
        v_temp = self.u(cNrm_temp) + EndOfPrdvFunc(aNrm_temp, Share_temp)
        vNvrs_temp = self.uinv(v_temp)
        vNvrsP_temp = self.uP(cNrm_temp) * self.uinvP(v_temp)
        vNvrsFuncAdj = CubicInterp(
            np.insert(mNrm_temp, 0, 0.0),  # x_list
            np.insert(vNvrs_temp, 0, 0.0),  # f_list
            np.insert(vNvrsP_temp, 0, vNvrsP_temp[0]),  # dfdx_list
        )
        # Re-curve the pseudo-inverse value function
        self.vFuncAdj_now = ValueFuncCRRA(vNvrsFuncAdj, self.CRRA)

        # Construct the value function when the agent *can't* adjust his portfolio
        mNrm_temp, Share_temp = np.meshgrid(self.aXtraGrid, self.ShareGrid)
        cNrm_temp = self.cFuncFxd_now(mNrm_temp, Share_temp)
        aNrm_temp = mNrm_temp - cNrm_temp
        v_temp = self.u(cNrm_temp) + EndOfPrdvFunc(aNrm_temp, Share_temp)
        vNvrs_temp = self.uinv(v_temp)
        vNvrsP_temp = self.uP(cNrm_temp) * self.uinvP(v_temp)
        vNvrsFuncFxd_by_Share = []
        for j in range(self.ShareCount):
            vNvrsFuncFxd_by_Share.append(
                CubicInterp(
                    np.insert(mNrm_temp[:, 0], 0, 0.0),  # x_list
                    np.insert(vNvrs_temp[:, j], 0, 0.0),  # f_list
                    np.insert(vNvrsP_temp[:, j], 0, vNvrsP_temp[j, 0]),  # dfdx_list
                )
            )
        vNvrsFuncFxd = LinearInterpOnInterp1D(vNvrsFuncFxd_by_Share, self.ShareGrid)
        self.vFuncFxd_now = ValueFuncCRRA(vNvrsFuncFxd, self.CRRA)

    def make_porfolio_solution(self):
        self.solution = PortfolioSolution(
            cFuncAdj=self.cFuncAdj_now,
            ShareFuncAdj=self.ShareFuncAdj_now,
            vPfuncAdj=self.vPfuncAdj_now,
            vFuncAdj=self.vFuncAdj_now,
            cFuncFxd=self.cFuncFxd_now,
            ShareFuncFxd=self.ShareFuncFxd_now,
            dvdmFuncFxd=self.dvdmFuncFxd_now,
            dvdsFuncFxd=self.dvdsFuncFxd_now,
            vFuncFxd=self.vFuncFxd_now,
            aGrid=self.save_points["a"],
            Share_adj=self.save_points["share_adj"],
            EndOfPrddvda_adj=self.save_points["eop_dvda_adj"],
            ShareGrid=self.save_points["share_grid"],
            EndOfPrddvda_fxd=self.save_points["eop_dvda_fxd"],
            EndOfPrddvds_fxd=self.save_points["eop_dvds_fxd"],
            AdjPrb=self.AdjustPrb,
        )

    def solve(self):
        """
        Solve the one period problem for a portfolio-choice consumer.

        Returns
        -------
        solution_now : PortfolioSolution
        The solution to the single period consumption-saving with portfolio choice
        problem.  Includes two consumption and risky share functions: one for when
        the agent can adjust his portfolio share (Adj) and when he can't (Fxd).
        """

        # Make arrays of end-of-period assets and end-of-period marginal values
        self.prepare_to_calc_EndOfPrdvP()
        self.calc_EndOfPrdvP()

        # Construct a basic solution for this period
        self.optimize_share()
        self.make_basic_solution()
        self.make_ShareFuncAdj()

        self.add_save_points()

        # Add the value function if requested
        if self.vFuncBool:
            self.add_vFunc()
        else:  # If vFuncBool is False, fill in dummy values
            self.vFuncAdj_now = NullFunc()
            self.vFuncFxd_now = NullFunc()

        self.make_porfolio_solution()

        return self.solution


class ConsPortfolioDiscreteSolver(ConsPortfolioSolver):
    """
    Define an object-oriented one period solver.
    Solve the one period problem for a portfolio-choice consumer.
    This solver is used when the income and risky return shocks
    are independent and the allowed optimal share is discrete
    over a finite set of points in ShareGrid.
    """

    def optimize_share(self):
        """
        Optimization of Share on the discrete set ShareGrid
        """

        opt_idx = np.argmax(self.EndOfPrdv, axis=1)
        # Best portfolio share is one with highest value
        self.Share_now = self.ShareGrid[opt_idx]
        # Take cNrm at that index as well
        self.cNrmAdj_now = self.EndOfPrddvdaNvrs[np.arange(self.aNrmCount), opt_idx]
        if not self.zero_bound:
            # aNrm=0, so there's no way to "optimize" the portfolio
            self.Share_now[0] = 1.0
            # Consumption when aNrm=0 does not depend on Share
            self.cNrmAdj_now[0] = self.EndOfPrddvdaNvrs[0, -1]

    def make_ShareFuncAdj(self):
        """
        Construct the risky share function when the agent can adjust
        """

        mNrmAdj_mid = (self.mNrmAdj_now[1:] + self.mNrmAdj_now[:-1]) / 2
        mNrmAdj_plus = mNrmAdj_mid * (1.0 + 1e-12)
        mNrmAdj_comb = (np.transpose(np.vstack((mNrmAdj_mid, mNrmAdj_plus)))).flatten()
        mNrmAdj_comb = np.append(np.insert(mNrmAdj_comb, 0, 0.0), self.mNrmAdj_now[-1])
        Share_comb = (
            np.transpose(np.vstack((self.Share_now, self.Share_now)))
        ).flatten()
        self.ShareFuncAdj_now = LinearInterp(mNrmAdj_comb, Share_comb)

    def solve(self):
        """
        Solve the one period problem for a portfolio-choice consumer.

        Returns
        -------
        solution_now : PortfolioSolution
        The solution to the single period consumption-saving with portfolio choice
        problem.  Includes two consumption and risky share functions: one for when
        the agent can adjust his portfolio share (Adj) and when he can't (Fxd).
        """

        # Make arrays of end-of-period assets and end-of-period marginal value
        self.prepare_to_calc_EndOfPrdvP()
        self.calc_EndOfPrdvP()

        # Construct a basic solution for this period
        self.make_EndOfPrdvFunc()
        self.optimize_share()
        self.make_basic_solution()
        self.make_ShareFuncAdj()

        self.add_save_points()

        self.make_vFunc()

        self.make_porfolio_solution()

        return self.solution


class ConsPortfolioJointDistSolver(ConsPortfolioDiscreteSolver, ConsPortfolioSolver):
    """
    Define an object-oriented one period solver.
    Solve the one period problem for a portfolio-choice consumer.
    This solver is used when the income and risky return shocks
    are not independent. The optimal share can be continuous or
    discrete.
    """

    def set_and_update_values(self):
        """
        Unpacks some of the inputs (and calculates simple objects based on them),
        storing the results in self for use by other methods.
        """

        # Unpack next period's solution
        self.vPfuncAdj_next = self.solution_next.vPfuncAdj
        self.dvdmFuncFxd_next = self.solution_next.dvdmFuncFxd
        self.dvdsFuncFxd_next = self.solution_next.dvdsFuncFxd
        self.vFuncAdj_next = self.solution_next.vFuncAdj
        self.vFuncFxd_next = self.solution_next.vFuncFxd

        # If the distributions are NOT independent...
        # Unpack the shock distribution
        self.TranShks_next = self.ShockDstn.atoms[1]
        # Flag for whether the natural borrowing constraint is zero
        self.zero_bound = np.min(self.TranShks_next) == 0.0

    def prepare_to_calc_EndOfPrdvP(self):
        """
        Prepare to calculate end-of-period marginal values by creating an array
        of market resources that the agent could have next period, considering
        the grid of end-of-period assets and the distribution of shocks he might
        experience next period.
        """

        # Make tiled arrays to calculate future realizations of mNrm and Share; dimension order: mNrm, Share, shock
        if self.zero_bound:
            self.aNrmGrid = self.aXtraGrid
        else:
            # Add an asset point at exactly zero
            self.aNrmGrid = np.insert(self.aXtraGrid, 0, 0.0)

        self.aNrmCount = self.aNrmGrid.size
        self.ShareCount = self.ShareGrid.size

        self.aNrm_tiled, self.Share_tiled = np.meshgrid(
            self.aNrmGrid, self.ShareGrid, indexing="ij"
        )

    def r_port(self, shocks, share):
        """
        Calculate future realizations of market resources
        """

        return (1.0 - share) * self.Rfree + share * shocks["Risky"]

    def m_nrm_next(self, shocks, a_nrm, r_port):
        """
        Calculate future realizations of market resources
        """

        return (
            r_port * a_nrm / (shocks["PermShk"] * self.PermGroFac) + shocks["TranShk"]
        )

    def calc_EndOfPrdvP(self):
        """
        Calculate end-of-period marginal value of assets and shares at each point
        in aNrm and ShareGrid. Does so by taking expectation of next period marginal
        values across income and risky return shocks.
        """

        def dvdm(m_nrm_next, shares):
            """
            Evaluate realizations of marginal value of market resources next period
            """

            dvdmAdj_next = self.vPfuncAdj_next(m_nrm_next)
            if self.AdjustPrb < 1.0:
                dvdmFxd_next = self.dvdmFuncFxd_next(m_nrm_next, shares)
                # Combine by adjustment probability
                dvdm_next = (
                    self.AdjustPrb * dvdmAdj_next
                    + (1.0 - self.AdjustPrb) * dvdmFxd_next
                )
            else:  # Don't bother evaluating if there's no chance that portfolio share is fixed
                dvdm_next = dvdmAdj_next

            return dvdm_next

        def dvds(m_nrm_next, shares):
            """
            Evaluate realizations of marginal value of risky share next period
            """

            # No marginal value of Share if it's a free choice!
            dvdsAdj_next = np.zeros_like(m_nrm_next)
            if self.AdjustPrb < 1.0:
                dvdsFxd_next = self.dvdsFuncFxd_next(m_nrm_next, shares)
                # Combine by adjustment probability
                dvds_next = (
                    self.AdjustPrb * dvdsAdj_next
                    + (1.0 - self.AdjustPrb) * dvdsFxd_next
                )
            else:  # Don't bother evaluating if there's no chance that portfolio share is fixed
                dvds_next = dvdsAdj_next

            return dvds_next

        def EndOfPrddvda_dists(shocks, a_nrm, shares):
            r_port = self.r_port(shocks, shares)
            m_nrm_next = self.m_nrm_next(shocks, a_nrm, r_port)

            # Expand shares to the shape of m so that operations can be vectorized
            shares_expanded = shares + np.zeros_like(m_nrm_next)

            return (
                r_port
                * self.uP(shocks["PermShk"] * self.PermGroFac)
                * dvdm(m_nrm_next, shares_expanded)
            )

        def EndOfPrddvds_dist(shocks, a_nrm, shares):
            Rxs = shocks["Risky"] - self.Rfree
            r_port = self.r_port(shocks, shares)
            m_nrm_next = self.m_nrm_next(shocks, a_nrm, r_port)

            return Rxs * a_nrm * self.uP(shocks["PermShk"] * self.PermGroFac) * dvdm(
                m_nrm_next, shares
            ) + (shocks["PermShk"] * self.PermGroFac) ** (1.0 - self.CRRA) * dvds(
                m_nrm_next, shares
            )

        # Calculate end-of-period marginal value of assets by taking expectations
        self.EndOfPrddvda = (
            self.DiscFac
            * self.LivPrb
            * self.ShockDstn.expected(
                EndOfPrddvda_dists, self.aNrm_tiled, self.Share_tiled
            )
        )

        self.EndOfPrddvdaNvrs = self.uPinv(self.EndOfPrddvda)

        # Calculate end-of-period marginal value of risky portfolio share by taking expectations
        self.EndOfPrddvds = (
            self.DiscFac
            * self.LivPrb
            * self.ShockDstn.expected(
                EndOfPrddvds_dist, self.aNrm_tiled, self.Share_tiled
            )
        )

    def make_EndOfPrdvFunc(self):
        """
        Construct the end-of-period value function for this period, storing it
        as an attribute of self for use by other methods.
        """

        def v_dist(shocks, a_nrm, shares):
            r_port = self.r_port(shocks, shares)
            m_nrm_next = self.m_nrm_next(shocks, a_nrm, r_port)

            vAdj_next = self.vFuncAdj_next(m_nrm_next)
            if self.AdjustPrb < 1.0:
                vFxd_next = self.vFuncFxd_next(m_nrm_next, shares)
                v_next = self.AdjustPrb * vAdj_next + (1.0 - self.AdjustPrb) * vFxd_next
            else:  # Don't bother evaluating if there's no chance that portfolio share is fixed
                v_next = vAdj_next

            return (shocks["PermShk"] * self.PermGroFac) ** (1.0 - self.CRRA) * v_next

        self.EndOfPrdv = (
            self.DiscFac
            * self.LivPrb
            * self.ShockDstn.expected(v_dist, self.aNrm_tiled, self.Share_tiled)
        )

        self.EndOfPrdvNvrs = self.uinv(self.EndOfPrdv)

    def solve(self):
        """
        Solve the one period problem for a portfolio-choice consumer.

        Returns
        -------
        solution_now : PortfolioSolution
        The solution to the single period consumption-saving with portfolio choice
        problem.  Includes two consumption and risky share functions: one for when
        the agent can adjust his portfolio share (Adj) and when he can't (Fxd).
        """

        # Make arrays of end-of-period assets and end-of-period marginal value
        self.prepare_to_calc_EndOfPrdvP()
        self.calc_EndOfPrdvP()

        if self.DiscreteShareBool:
            self.make_EndOfPrdvFunc()
            ConsPortfolioDiscreteSolver.optimize_share(self)

            # Construct a basic solution for this period
            self.make_basic_solution()
            ConsPortfolioDiscreteSolver.make_ShareFuncAdj(self)
            self.make_vFunc()
        else:
            # Construct a basic solution for this period
            ConsPortfolioSolver.optimize_share(self)
            self.make_basic_solution()
            ConsPortfolioSolver.make_ShareFuncAdj(self)

            # Add the value function if requested
            if self.vFuncBool:
                self.add_vFunc()
            else:  # If vFuncBool is False, fill in dummy values
                self.vFuncAdj_now = NullFunc()
                self.vFuncFxd_now = NullFunc()

        self.add_save_points()

        self.make_porfolio_solution()

        return self.solution


class ConsSequentialPortfolioSolver(ConsPortfolioSolver):
    def add_SequentialShareFuncAdj(self, solution):
        """
        Construct the risky share function as a function of savings when the agent can adjust.
        """

        if self.zero_bound:
            Share_lower_bound = self.ShareLimit
            aNrm_temp = np.insert(self.aNrmGrid, 0, 0.0)
            Share_now = np.insert(self.Share_now, 0, Share_lower_bound)
        else:
            aNrm_temp = self.aNrmGrid  # already includes 0.0
            Share_now = self.Share_now

        self.SequentialShareFuncAdj_now = LinearInterp(
            aNrm_temp,
            Share_now,
            intercept_limit=self.ShareLimit,
            slope_limit=0.0,
        )

        solution.SequentialShareFuncAdj = self.SequentialShareFuncAdj_now

        return solution

    def solve(self):
        solution = ConsPortfolioSolver.solve(self)

        solution = self.add_SequentialShareFuncAdj(solution)

        return solution


##############################################################################


class BequestWarmGlowConsumerSolver(ConsIndShockSolver):
    def __init__(
        self,
        solution_next,
        IncShkDstn,
        LivPrb,
        DiscFac,
        CRRA,
        Rfree,
        PermGroFac,
        BoroCnstArt,
        aXtraGrid,
        BeqCRRA,
        BeqFac,
        BeqShift,
    ):
        self.BeqCRRA = BeqCRRA
        self.BeqFac = BeqFac
        self.BeqShift = BeqShift
        vFuncBool = False
        CubicBool = False

        super().__init__(
            solution_next,
            IncShkDstn,
            LivPrb,
            DiscFac,
            CRRA,
            Rfree,
            PermGroFac,
            BoroCnstArt,
            aXtraGrid,
            vFuncBool,
            CubicBool,
        )

    def def_utility_funcs(self):
        super().def_utility_funcs()

        BeqFacEff = (1.0 - self.LivPrb) * self.BeqFac

        self.warm_glow = UtilityFuncStoneGeary(self.BeqCRRA, BeqFacEff, self.BeqShift)

    def def_BoroCnst(self, BoroCnstArt):
        self.BoroCnstNat = (
            (self.solution_next.mNrmMin - self.TranShkMinNext)
            * (self.PermGroFac * self.PermShkMinNext)
            / self.Rfree
        )

        self.BoroCnstNat = np.max([self.BoroCnstNat, -self.BeqShift])

        if BoroCnstArt is None:
            self.mNrmMinNow = self.BoroCnstNat
        else:
            self.mNrmMinNow = np.max([self.BoroCnstNat, BoroCnstArt])
        if self.BoroCnstNat < self.mNrmMinNow:
            self.MPCmaxEff = 1.0
        else:
            self.MPCmaxEff = self.MPCmaxNow

        self.cFuncNowCnst = LinearInterp(
            np.array([self.mNrmMinNow, self.mNrmMinNow + 1]), np.array([0.0, 1.0])
        )

    def calc_EndOfPrdvP(self):
        EndofPrdvP = super().calc_EndOfPrdvP()

        return EndofPrdvP + self.warm_glow.der(self.aNrmNow)


class BequestWarmGlowPortfolioSolver(ConsPortfolioSolver):
    def __init__(
        self,
        solution_next,
        ShockDstn,
        IncShkDstn,
        RiskyDstn,
        LivPrb,
        DiscFac,
        CRRA,
        Rfree,
        PermGroFac,
        BoroCnstArt,
        aXtraGrid,
        ShareGrid,
        AdjustPrb,
        ShareLimit,
        BeqCRRA,
        BeqFac,
        BeqShift,
    ):
        self.BeqCRRA = BeqCRRA
        self.BeqFac = BeqFac
        self.BeqShift = BeqShift
        vFuncBool = False
        DiscreteShareBool = False
        IndepDstnBool = True

        super().__init__(
            solution_next,
            ShockDstn,
            IncShkDstn,
            RiskyDstn,
            LivPrb,
            DiscFac,
            CRRA,
            Rfree,
            PermGroFac,
            BoroCnstArt,
            aXtraGrid,
            ShareGrid,
            vFuncBool,
            AdjustPrb,
            DiscreteShareBool,
            ShareLimit,
            IndepDstnBool,
        )

    def def_utility_funcs(self):
        super().def_utility_funcs()
        BeqFacEff = (1.0 - self.LivPrb) * self.BeqFac  # "effective" beq factor
        self.warm_glow = UtilityFuncStoneGeary(self.BeqCRRA, BeqFacEff, self.BeqShift)

    def calc_EndOfPrdvP(self):
        super().calc_EndOfPrdvP()

        self.EndOfPrddvda = self.EndOfPrddvda + self.warm_glow.der(self.aNrm_tiled)
        self.EndOfPrddvdaNvrs = self.uPinv(self.EndOfPrddvda)


##############################################################################


class ConsMarkovSolver(ConsIndShockSolver):
    """
    A class to solve a single period consumption-saving problem with risky income
    and stochastic transitions between discrete states, in a Markov fashion.
    Extends ConsIndShockSolver, with identical inputs but for a discrete
    Markov state, whose transition rule is summarized in MrkvArray.  Markov
    states can differ in their interest factor, permanent growth factor, live probability, and
    income distribution, so the inputs Rfree, PermGroFac, IncShkDstn, and LivPrb are
    now arrays or lists specifying those values in each (succeeding) Markov state.
    """

    def __init__(
        self,
        solution_next,
        IncShkDstn_list,
        LivPrb,
        DiscFac,
        CRRA,
        Rfree_list,
        PermGroFac_list,
        MrkvArray,
        BoroCnstArt,
        aXtraGrid,
        vFuncBool,
        CubicBool,
    ):
        """
        Constructor for a new solver for a one period problem with risky income
        and transitions between discrete Markov states.  In the descriptions below,
        N is the number of discrete states.

        Parameters
        ----------
        solution_next : ConsumerSolution
            The solution to next period's one period problem.
        IncShkDstn_list : [distribution.Distribution]
            A length N list of income distributions in each succeeding Markov
            state.  Each income distribution is a
            discrete approximation to the income process at the
            beginning of the succeeding period.
        LivPrb : np.array
            Survival probability; likelihood of being alive at the beginning of
            the succeeding period for each Markov state.
        DiscFac : float
            Intertemporal discount factor for future utility.
        CRRA : float
            Coefficient of relative risk aversion.
        Rfree_list : np.array
            Risk free interest factor on end-of-period assets for each Markov
            state in the succeeding period.
        PermGroFac_list : np.array
            Expected permanent income growth factor at the end of this period
            for each Markov state in the succeeding period.
        MrkvArray : np.array
            An NxN array representing a Markov transition matrix between discrete
            states.  The i,j-th element of MrkvArray is the probability of
            moving from state i in period t to state j in period t+1.
        BoroCnstArt: float or None
            Borrowing constraint for the minimum allowable assets to end the
            period with.  If it is less than the natural borrowing constraint,
            then it is irrelevant; BoroCnstArt=None indicates no artificial bor-
            rowing constraint.
        aXtraGrid: np.array
            Array of "extra" end-of-period asset values-- assets above the
            absolute minimum acceptable level.
        vFuncBool: boolean
            An indicator for whether the value function should be computed and
            included in the reported solution.
        CubicBool: boolean
            An indicator for whether the solver should use cubic or linear inter-
            polation.

        Returns
        -------
        None
        """
        # Set basic attributes of the problem

        self.solution_next = solution_next
        self.IncShkDstn_list = IncShkDstn_list
        self.LivPrb = LivPrb
        self.DiscFac = DiscFac
        self.CRRA = CRRA
        self.BoroCnstArt = BoroCnstArt
        self.aXtraGrid = aXtraGrid
        self.vFuncBool = vFuncBool
        self.CubicBool = CubicBool
        self.Rfree_list = Rfree_list
        self.PermGroFac_list = PermGroFac_list
        self.MrkvArray = MrkvArray
        self.StateCount = MrkvArray.shape[0]

        self.def_utility_funcs()

    def solve(self):
        """
        Solve the one period problem of the consumption-saving model with a Markov state.

        Parameters
        ----------
        none

        Returns
        -------
        solution : ConsumerSolution
            The solution to the single period consumption-saving problem. Includes
            a consumption function cFunc (using cubic or linear splines), a marg-
            inal value function vPfunc, a minimum acceptable level of normalized
            market resources mNrmMin, normalized human wealth hNrm, and bounding
            MPCs MPCmin and MPCmax.  It might also have a value function vFunc
            and marginal marginal value function vPPfunc.  All of these attributes
            are lists or arrays, with elements corresponding to the current
            Markov state.  E.g. solution.cFunc[0] is the consumption function
            when in the i=0 Markov state this period.
        """
        # Find the natural borrowing constraint in each current state
        self.def_boundary()

        # Initialize end-of-period (marginal) value functions
        self.EndOfPrdvFunc_list = []
        self.EndOfPrdvPfunc_list = []
        self.Ex_IncNextAll = (
            np.zeros(self.StateCount) + np.nan
        )  # expected income conditional on the next state
        self.WorstIncPrbAll = (
            np.zeros(self.StateCount) + np.nan
        )  # probability of getting the worst income shock in each next period state

        # Loop through each next-period-state and calculate the end-of-period
        # (marginal) value function
        for j in range(self.StateCount):
            # Condition values on next period's state (and record a couple for later use)
            self.condition_on_state(j)
            self.Ex_IncNextAll[j] = np.dot(
                self.ShkPrbsNext, self.PermShkValsNext * self.TranShkValsNext
            )
            self.WorstIncPrbAll[j] = self.WorstIncPrb

            # Construct the end-of-period marginal value function conditional
            # on next period's state and add it to the list of value functions
            EndOfPrdvPfunc_cond = self.make_EndOfPrdvPfuncCond()
            self.EndOfPrdvPfunc_list.append(EndOfPrdvPfunc_cond)

            # Construct the end-of-period value functional conditional on next
            # period's state and add it to the list of value functions
            if self.vFuncBool:
                EndOfPrdvFunc_cond = self.make_EndOfPrdvFuncCond()
                self.EndOfPrdvFunc_list.append(EndOfPrdvFunc_cond)

        # EndOfPrdvP_cond is EndOfPrdvP conditional on *next* period's state.
        # Take expectations to get EndOfPrdvP conditional on *this* period's state.
        self.calc_EndOfPrdvP()

        # Calculate the bounding MPCs and PDV of human wealth for each state
        self.calc_HumWealth_and_BoundingMPCs()

        # Find consumption and market resources corresponding to each end-of-period
        # assets point for each state (and add an additional point at the lower bound)
        aNrm = (
            np.asarray(self.aXtraGrid)[np.newaxis, :]
            + np.array(self.BoroCnstNat_list)[:, np.newaxis]
        )
        self.get_points_for_interpolation(self.EndOfPrdvP, aNrm)
        cNrm = np.hstack((np.zeros((self.StateCount, 1)), self.cNrmNow))
        mNrm = np.hstack(
            (np.reshape(self.mNrmMin_list, (self.StateCount, 1)), self.mNrmNow)
        )

        # Package and return the solution for this period
        self.BoroCnstNat = self.BoroCnstNat_list
        solution = self.make_solution(cNrm, mNrm)
        return solution

    def def_boundary(self):
        """
        Find the borrowing constraint for each current state and save it as an
        attribute of self for use by other methods.

        Parameters
        ----------
        none

        Returns
        -------
        none
        """
        self.BoroCnstNatAll = np.zeros(self.StateCount) + np.nan
        # Find the natural borrowing constraint conditional on next period's state
        for j in range(self.StateCount):
            PermShkMinNext = np.min(self.IncShkDstn_list[j].atoms[0])
            TranShkMinNext = np.min(self.IncShkDstn_list[j].atoms[1])
            self.BoroCnstNatAll[j] = (
                (self.solution_next.mNrmMin[j] - TranShkMinNext)
                * (self.PermGroFac_list[j] * PermShkMinNext)
                / self.Rfree_list[j]
            )

        self.BoroCnstNat_list = np.zeros(self.StateCount) + np.nan
        self.mNrmMin_list = np.zeros(self.StateCount) + np.nan
        self.BoroCnstDependency = np.zeros((self.StateCount, self.StateCount)) + np.nan
        # The natural borrowing constraint in each current state is the *highest*
        # among next-state-conditional natural borrowing constraints that could
        # occur from this current state.
        for i in range(self.StateCount):
            possible_next_states = self.MrkvArray[i, :] > 0
            self.BoroCnstNat_list[i] = np.max(self.BoroCnstNatAll[possible_next_states])

            # Explicitly handle the "None" case:
            if self.BoroCnstArt is None:
                self.mNrmMin_list[i] = self.BoroCnstNat_list[i]
            else:
                self.mNrmMin_list[i] = np.max(
                    [self.BoroCnstNat_list[i], self.BoroCnstArt]
                )
            self.BoroCnstDependency[i, :] = (
                self.BoroCnstNat_list[i] == self.BoroCnstNatAll
            )
        # Also creates a Boolean array indicating whether the natural borrowing
        # constraint *could* be hit when transitioning from i to j.

    def condition_on_state(self, state_index):
        """
        Temporarily assume that a particular Markov state will occur in the
        succeeding period, and condition solver attributes on this assumption.
        Allows the solver to construct the future-state-conditional marginal
        value function (etc) for that future state.

        Parameters
        ----------
        state_index : int
            Index of the future Markov state to condition on.

        Returns
        -------
        none
        """
        # Set future-state-conditional values as attributes of self
        self.IncShkDstn = self.IncShkDstn_list[state_index]
        self.Rfree = self.Rfree_list[state_index]
        self.PermGroFac = self.PermGroFac_list[state_index]
        self.vPfuncNext = self.solution_next.vPfunc[state_index]
        self.mNrmMinNow = self.mNrmMin_list[state_index]
        self.BoroCnstNat = self.BoroCnstNatAll[state_index]
        self.set_and_update_values(
            self.solution_next, self.IncShkDstn, self.LivPrb, self.DiscFac
        )
        self.DiscFacEff = (
            self.DiscFac
        )  # survival probability LivPrb represents probability from
        # *current* state, so DiscFacEff is just DiscFac for now

        # These lines have to come after set_and_update_values to override the definitions there
        self.vPfuncNext = self.solution_next.vPfunc[state_index]
        if self.CubicBool:
            self.vPPfuncNext = self.solution_next.vPPfunc[state_index]
        if self.vFuncBool:
            self.vFuncNext = self.solution_next.vFunc[state_index]

    def calc_EndOfPrdvPP(self):
        """
        Calculates end-of-period marginal marginal value using a pre-defined
        array of next period market resources in self.mNrmNext.

        Parameters
        ----------
        none

        Returns
        -------
        EndOfPrdvPP : np.array
            End-of-period marginal marginal value of assets at each value in
            the grid of assets.
        """

        def vpp_next(shocks, a_nrm, Rfree):
            return shocks["PermShk"] ** (-self.CRRA - 1.0) * self.vPPfuncNext(
                self.m_nrm_next(shocks, a_nrm, Rfree)
            )

        EndOfPrdvPP = (
            self.DiscFacEff
            * self.Rfree
            * self.Rfree
            * self.PermGroFac ** (-self.CRRA - 1.0)
            * self.IncShkDstn.expected(vpp_next, self.aNrmNow, self.Rfree)
        )
        return EndOfPrdvPP

    def make_EndOfPrdvFuncCond(self):
        """
        Construct the end-of-period value function conditional on next period's
        state.

        Parameters
        ----------
        EndOfPrdvP : np.array
            Array of end-of-period marginal value of assets corresponding to the
            asset values in self.aNrmNow.
        Returns
        -------
        none
        """

        def v_lvl_next(shocks, a_nrm, Rfree):
            return (
                shocks["PermShk"] ** (1.0 - self.CRRA)
                * self.PermGroFac ** (1.0 - self.CRRA)
            ) * self.vFuncNext(self.m_nrm_next(shocks, a_nrm, Rfree))

        EndOfPrdv_cond = self.DiscFacEff * self.IncShkDstn.expected(
            v_lvl_next, self.aNrmNow, self.Rfree
        )
        EndOfPrdvNvrs = self.u.inv(
            EndOfPrdv_cond
        )  # value transformed through inverse utility
        EndOfPrdvNvrsP = self.EndOfPrdvP_cond * self.u.derinv(
            EndOfPrdv_cond, order=(0, 1)
        )
        EndOfPrdvNvrs = np.insert(EndOfPrdvNvrs, 0, 0.0)
        EndOfPrdvNvrsP = np.insert(
            EndOfPrdvNvrsP, 0, EndOfPrdvNvrsP[0]
        )  # This is a very good approximation, vNvrsPP = 0 at the asset minimum
        aNrm_temp = np.insert(self.aNrmNow, 0, self.BoroCnstNat)
        EndOfPrdvNvrsFunc = CubicInterp(aNrm_temp, EndOfPrdvNvrs, EndOfPrdvNvrsP)
        EndOfPrdvFunc_cond = ValueFuncCRRA(EndOfPrdvNvrsFunc, self.CRRA)

        return EndOfPrdvFunc_cond

    def calc_EndOfPrdvPcond(self):
        """
        Calculate end-of-period marginal value of assets at each point in aNrmNow
        conditional on a particular state occuring in the next period.

        Parameters
        ----------
        None

        Returns
        -------
        EndOfPrdvP : np.array
            A 1D array of end-of-period marginal value of assets.
        """
        EndOfPrdvPcond = ConsIndShockSolver.calc_EndOfPrdvP(self)
        return EndOfPrdvPcond

    def make_EndOfPrdvPfuncCond(self):
        """
        Construct the end-of-period marginal value function conditional on next
        period's state.

        Parameters
        ----------
        None

        Returns
        -------
        EndofPrdvPfunc_cond : MargValueFuncCRRA
            The end-of-period marginal value function conditional on a particular
            state occuring in the succeeding period.
        """
        # Get data to construct the end-of-period marginal value function (conditional on next state)
        self.aNrm_cond = self.prepare_to_calc_EndOfPrdvP()
        self.EndOfPrdvP_cond = self.calc_EndOfPrdvPcond()
        EndOfPrdvPnvrs_cond = self.u.derinv(
            self.EndOfPrdvP_cond, order=(1, 0)
        )  # "decurved" marginal value
        if self.CubicBool:
            EndOfPrdvPP_cond = self.calc_EndOfPrdvPP()
            EndOfPrdvPnvrsP_cond = EndOfPrdvPP_cond * self.u.derinv(
                self.EndOfPrdvP_cond, order=(1, 1)
            )  # "decurved" marginal marginal value

        # Construct the end-of-period marginal value function conditional on the next state.
        if self.CubicBool:
            EndOfPrdvPnvrsFunc_cond = CubicInterp(
                self.aNrm_cond,
                EndOfPrdvPnvrs_cond,
                EndOfPrdvPnvrsP_cond,
                lower_extrap=True,
            )
        else:
            EndOfPrdvPnvrsFunc_cond = LinearInterp(
                self.aNrm_cond, EndOfPrdvPnvrs_cond, lower_extrap=True
            )
        EndofPrdvPfunc_cond = MargValueFuncCRRA(
            EndOfPrdvPnvrsFunc_cond, self.CRRA
        )  # "recurve" the interpolated marginal value function
        return EndofPrdvPfunc_cond

    def calc_EndOfPrdvP(self):
        """
        Calculates end of period marginal value (and marginal marginal) value
        at each aXtra gridpoint for each current state, unconditional on the
        future Markov state (i.e. weighting conditional end-of-period marginal
        value by transition probabilities).

        Parameters
        ----------
        none

        Returns
        -------
        none
        """
        # Find unique values of minimum acceptable end-of-period assets (and the
        # current period states for which they apply).
        aNrmMin_unique, state_inverse = np.unique(
            self.BoroCnstNat_list, return_inverse=True
        )
        self.possible_transitions = self.MrkvArray > 0

        # Calculate end-of-period marginal value (and marg marg value) at each
        # asset gridpoint for each current period state
        EndOfPrdvP = np.zeros((self.StateCount, self.aXtraGrid.size))
        EndOfPrdvPP = np.zeros((self.StateCount, self.aXtraGrid.size))
        for k in range(aNrmMin_unique.size):
            aNrmMin = aNrmMin_unique[k]  # minimum assets for this pass
            which_states = (
                state_inverse == k
            )  # the states for which this minimum applies
            aGrid = aNrmMin + self.aXtraGrid  # assets grid for this pass
            EndOfPrdvP_all = np.zeros((self.StateCount, self.aXtraGrid.size))
            EndOfPrdvPP_all = np.zeros((self.StateCount, self.aXtraGrid.size))
            for j in range(self.StateCount):
                if np.any(
                    np.logical_and(self.possible_transitions[:, j], which_states)
                ):  # only consider a future state if one of the relevant states could transition to it
                    EndOfPrdvP_all[j, :] = self.EndOfPrdvPfunc_list[j](aGrid)
                    # Add conditional end-of-period (marginal) marginal value to the arrays
                    if self.CubicBool:
                        EndOfPrdvPP_all[j, :] = self.EndOfPrdvPfunc_list[j].derivativeX(
                            aGrid
                        )
            # Weight conditional marginal (marginal) values by transition probs
            # to get unconditional marginal (marginal) value at each gridpoint.
            EndOfPrdvP_temp = np.dot(self.MrkvArray, EndOfPrdvP_all)
            EndOfPrdvP[which_states, :] = EndOfPrdvP_temp[
                which_states, :
            ]  # only take the states for which this asset minimum applies
            if self.CubicBool:
                EndOfPrdvPP_temp = np.dot(self.MrkvArray, EndOfPrdvPP_all)
                EndOfPrdvPP[which_states, :] = EndOfPrdvPP_temp[which_states, :]

        # Store the results as attributes of self, scaling end of period marginal value by survival probability from each current state
        LivPrb_tiled = np.tile(
            np.reshape(self.LivPrb, (self.StateCount, 1)), (1, self.aXtraGrid.size)
        )
        self.EndOfPrdvP = LivPrb_tiled * EndOfPrdvP
        if self.CubicBool:
            self.EndOfPrdvPP = LivPrb_tiled * EndOfPrdvPP

    def calc_HumWealth_and_BoundingMPCs(self):
        """
        Calculates human wealth and the maximum and minimum MPC for each current
        period state, then stores them as attributes of self for use by other methods.

        Parameters
        ----------
        none

        Returns
        -------
        none
        """
        # Upper bound on MPC at lower m-bound
        WorstIncPrb_array = self.BoroCnstDependency * np.tile(
            np.reshape(self.WorstIncPrbAll, (1, self.StateCount)), (self.StateCount, 1)
        )
        temp_array = self.MrkvArray * WorstIncPrb_array
        WorstIncPrbNow = np.sum(
            temp_array, axis=1
        )  # Probability of getting the "worst" income shock and transition from each current state
        ExMPCmaxNext = (
            np.dot(
                temp_array,
                self.Rfree_list ** (1.0 - self.CRRA)
                * self.solution_next.MPCmax ** (-self.CRRA),
            )
            / WorstIncPrbNow
        ) ** (-1.0 / self.CRRA)
        DiscFacEff_temp = self.DiscFac * self.LivPrb
        self.MPCmaxNow = 1.0 / (
            1.0
            + ((DiscFacEff_temp * WorstIncPrbNow) ** (1.0 / self.CRRA)) / ExMPCmaxNext
        )
        self.MPCmaxEff = self.MPCmaxNow
        self.MPCmaxEff[self.BoroCnstNat_list < self.mNrmMin_list] = 1.0
        # State-conditional PDV of human wealth
        hNrmPlusIncNext = self.Ex_IncNextAll + self.solution_next.hNrm
        self.hNrmNow = np.dot(
            self.MrkvArray, (self.PermGroFac_list / self.Rfree_list) * hNrmPlusIncNext
        )
        # Lower bound on MPC as m gets arbitrarily large
        temp = (
            DiscFacEff_temp
            * np.dot(
                self.MrkvArray,
                self.solution_next.MPCmin ** (-self.CRRA)
                * self.Rfree_list ** (1.0 - self.CRRA),
            )
        ) ** (1.0 / self.CRRA)
        self.MPCminNow = 1.0 / (1.0 + temp)

    def make_solution(self, cNrm, mNrm):
        """
        Construct an object representing the solution to this period's problem.

        Parameters
        ----------
        cNrm : np.array
            Array of normalized consumption values for interpolation.  Each row
            corresponds to a Markov state for this period.
        mNrm : np.array
            Array of normalized market resource values for interpolation.  Each
            row corresponds to a Markov state for this period.

        Returns
        -------
        solution : ConsumerSolution
            The solution to the single period consumption-saving problem. Includes
            a consumption function cFunc (using cubic or linear splines), a marg-
            inal value function vPfunc, a minimum acceptable level of normalized
            market resources mNrmMin, normalized human wealth hNrm, and bounding
            MPCs MPCmin and MPCmax.  It might also have a value function vFunc
            and marginal marginal value function vPPfunc.  All of these attributes
            are lists or arrays, with elements corresponding to the current
            Markov state.  E.g. solution.cFunc[0] is the consumption function
            when in the i=0 Markov state this period.
        """
        solution = (
            ConsumerSolution()
        )  # An empty solution to which we'll add state-conditional solutions
        # Calculate the MPC at each market resource gridpoint in each state (if desired)
        if self.CubicBool:
            dcda = self.EndOfPrdvPP / self.u.der(np.array(self.cNrmNow), order=2)
            MPC = dcda / (dcda + 1.0)
            self.MPC_temp = np.hstack(
                (np.reshape(self.MPCmaxNow, (self.StateCount, 1)), MPC)
            )
            interpfunc = self.make_cubic_cFunc
        else:
            interpfunc = self.make_linear_cFunc

        # Loop through each current period state and add its solution to the overall solution
        for i in range(self.StateCount):
            # Set current-period-conditional human wealth and MPC bounds
            self.hNrmNow_j = self.hNrmNow[i]
            self.MPCminNow_j = self.MPCminNow[i]
            if self.CubicBool:
                self.MPC_temp_j = self.MPC_temp[i, :]

            # Construct the consumption function by combining the constrained and unconstrained portions
            self.cFuncNowCnst = LinearInterp(
                [self.mNrmMin_list[i], self.mNrmMin_list[i] + 1.0], [0.0, 1.0]
            )
            cFuncNowUnc = interpfunc(mNrm[i, :], cNrm[i, :])
            cFuncNow = LowerEnvelope(cFuncNowUnc, self.cFuncNowCnst)

            # Make the marginal value function and pack up the current-state-conditional solution
            vPfuncNow = MargValueFuncCRRA(cFuncNow, self.CRRA)
            solution_cond = ConsumerSolution(
                cFunc=cFuncNow, vPfunc=vPfuncNow, mNrmMin=self.mNrmMinNow
            )
            if self.CubicBool:
                # Add the state-conditional marginal marginal value function (if desired)
                solution_cond = self.add_vPPfunc(solution_cond)

            # Add the current-state-conditional solution to the overall period solution
            solution.append_solution(solution_cond)

        # Add the lower bounds of market resources, MPC limits, human resources,
        # and the value functions to the overall solution
        solution.mNrmMin = self.mNrmMin_list
        solution = self.add_MPC_and_human_wealth(solution)
        if self.vFuncBool:
            vFuncNow = self.make_vFunc(solution)
            solution.vFunc = vFuncNow

        # Return the overall solution to this period
        return solution

    def make_linear_cFunc(self, mNrm, cNrm):
        """
        Make a linear interpolation to represent the (unconstrained) consumption
        function conditional on the current period state.

        Parameters
        ----------
        mNrm : np.array
            Array of normalized market resource values for interpolation.
        cNrm : np.array
            Array of normalized consumption values for interpolation.

        Returns
        -------
        cFuncUnc: an instance of HARK.interpolation.LinearInterp
        """
        cFuncUnc = LinearInterp(
            mNrm, cNrm, self.MPCminNow_j * self.hNrmNow_j, self.MPCminNow_j
        )
        return cFuncUnc

    def make_cubic_cFunc(self, mNrm, cNrm):
        """
        Make a cubic interpolation to represent the (unconstrained) consumption
        function conditional on the current period state.

        Parameters
        ----------
        mNrm : np.array
            Array of normalized market resource values for interpolation.
        cNrm : np.array
            Array of normalized consumption values for interpolation.

        Returns
        -------
        cFuncUnc: an instance of HARK.interpolation.CubicInterp
        """
        cFuncUnc = CubicInterp(
            mNrm,
            cNrm,
            self.MPC_temp_j,
            self.MPCminNow_j * self.hNrmNow_j,
            self.MPCminNow_j,
        )
        return cFuncUnc

    def make_vFunc(self, solution):
        """
        Construct the value function for each current state.

        Parameters
        ----------
        solution : ConsumerSolution
            The solution to the single period consumption-saving problem. Must
            have a consumption function cFunc (using cubic or linear splines) as
            a list with elements corresponding to the current Markov state.  E.g.
            solution.cFunc[0] is the consumption function when in the i=0 Markov
            state this period.

        Returns
        -------
        vFuncNow : [ValueFuncCRRA]
            A list of value functions (defined over normalized market resources
            m) for each current period Markov state.
        """
        vFuncNow = []  # Initialize an empty list of value functions
        # Loop over each current period state and construct the value function
        for i in range(self.StateCount):
            # Make state-conditional grids of market resources and consumption
            mNrmMin = self.mNrmMin_list[i]
            mGrid = mNrmMin + self.aXtraGrid
            cGrid = solution.cFunc[i](mGrid)
            aGrid = mGrid - cGrid

            # Calculate end-of-period value at each gridpoint
            EndOfPrdv_all = np.zeros((self.StateCount, self.aXtraGrid.size))
            for j in range(self.StateCount):
                if self.possible_transitions[i, j]:
                    EndOfPrdv_all[j, :] = self.EndOfPrdvFunc_list[j](aGrid)
            EndOfPrdv = np.dot(self.MrkvArray[i, :], EndOfPrdv_all)

            # Calculate (normalized) value and marginal value at each gridpoint
            vNrmNow = self.u(cGrid) + EndOfPrdv
            vPnow = self.u.der(cGrid)

            # Make a "decurved" value function with the inverse utility function
            # value transformed through inverse utility
            vNvrs = self.u.inv(vNrmNow)
            vNvrsP = vPnow * self.u.derinv(vNrmNow, order=(0, 1))
            mNrm_temp = np.insert(mGrid, 0, mNrmMin)  # add the lower bound
            vNvrs = np.insert(vNvrs, 0, 0.0)
            vNvrsP = np.insert(
                vNvrsP, 0, self.MPCmaxEff[i] ** (-self.CRRA / (1.0 - self.CRRA))
            )
            # MPCminNvrs = self.MPCminNow[i] ** (-self.CRRA / (1.0 - self.CRRA))
            vNvrsFunc_i = CubicInterp(
                mNrm_temp,
                vNvrs,
                vNvrsP,
            )  # MPCminNvrs * self.hNrmNow[i], MPCminNvrs

            # "Recurve" the decurved value function and add it to the list
            vFunc_i = ValueFuncCRRA(vNvrsFunc_i, self.CRRA)
            vFuncNow.append(vFunc_i)
        return vFuncNow


def _solve_ConsMarkov(
    solution_next,
    IncShkDstn,
    LivPrb,
    DiscFac,
    CRRA,
    Rfree,
    PermGroFac,
    MrkvArray,
    BoroCnstArt,
    aXtraGrid,
    vFuncBool,
    CubicBool,
):
    """
    Solves a single period consumption-saving problem with risky income and
    stochastic transitions between discrete states, in a Markov fashion.  Has
    identical inputs as solveConsIndShock, except for a discrete
    Markov transitionrule MrkvArray.  Markov states can differ in their interest
    factor, permanent growth factor, and income distribution, so the inputs Rfree,
    PermGroFac, and IncShkDstn are arrays or lists specifying those values in each
    (succeeding) Markov state.

    Parameters
    ----------
    solution_next : ConsumerSolution
        The solution to next period's one period problem.
    IncShkDstn_list : [distribution.Distribution]
        A length N list of income distributions in each succeeding Markov
        state.  Each income distribution is
        a discrete approximation to the income process at the
        beginning of the succeeding period.
    LivPrb : float
        Survival probability; likelihood of being alive at the beginning of
        the succeeding period.
    DiscFac : float
        Intertemporal discount factor for future utility.
    CRRA : float
        Coefficient of relative risk aversion.
    Rfree_list : np.array
        Risk free interest factor on end-of-period assets for each Markov
        state in the succeeding period.
    PermGroGac_list : float
        Expected permanent income growth factor at the end of this period
        for each Markov state in the succeeding period.
    MrkvArray : numpy.array
        An NxN array representing a Markov transition matrix between discrete
        states.  The i,j-th element of MrkvArray is the probability of
        moving from state i in period t to state j in period t+1.
    BoroCnstArt: float or None
        Borrowing constraint for the minimum allowable assets to end the
        period with.  If it is less than the natural borrowing constraint,
        then it is irrelevant; BoroCnstArt=None indicates no artificial bor-
        rowing constraint.
    aXtraGrid: np.array
        Array of "extra" end-of-period asset values-- assets above the
        absolute minimum acceptable level.
    vFuncBool: boolean
        An indicator for whether the value function should be computed and
        included in the reported solution.
    CubicBool: boolean
        An indicator for whether the solver should use cubic or linear inter-
        polation.

    Returns
    -------
    solution : ConsumerSolution
        The solution to the single period consumption-saving problem. Includes
        a consumption function cFunc (using cubic or linear splines), a marg-
        inal value function vPfunc, a minimum acceptable level of normalized
        market resources mNrmMin, normalized human wealth hNrm, and bounding
        MPCs MPCmin and MPCmax.  It might also have a value function vFunc
        and marginal marginal value function vPPfunc.  All of these attributes
        are lists or arrays, with elements corresponding to the current
        Markov state.  E.g. solution.cFunc[0] is the consumption function
        when in the i=0 Markov state this period.
    """
    solver = ConsMarkovSolver(
        solution_next,
        IncShkDstn,
        LivPrb,
        DiscFac,
        CRRA,
        Rfree,
        PermGroFac,
        MrkvArray,
        BoroCnstArt,
        aXtraGrid,
        vFuncBool,
        CubicBool,
    )
    solution_now = solver.solve()
    return solution_now
