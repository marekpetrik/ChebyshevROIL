from typing import List, Set, Tuple
import numpy as np
import itertools
import gurobipy as gp
from gurobipy import GRB
import scipy
import sklearn.linear_model

class MDP(object):
    """MDP class for use in the following methods which solve the MDP
    """

    def __init__(
        self,
        num_states: int,
        num_actions: int,
        num_features: int,
        P,
        phi,
        p_0,
        gamma,
        reward=None,
    ):
        """
        parameters:
            num_states: number of states in the MDP
            num_actions: number of actions in the MDP
            num_features: number of features in the MDP
            P: transition probability matrix of size S x S x A
            phi: feature matrix of size SA (rows) x K (cols)
            p_0: initial state distribution of size S
            gamma: discount factor
            reward: reward vector with SA entries
        """
        # super(MDP, self).__init__()
        self.num_states = num_states
        self.num_actions = num_actions
        self.states = np.arange(self.num_states)
        self.actions = np.arange(self.num_actions)
        self.num_features = num_features
        self.gamma = gamma  # discount factor
        # transition probability P \in S x S x A
        self.P = P
        # initial state distribution p_0 \in S
        self.p_0 = p_0
        # reward(S x A) vector itrating by state first
        self.reward = reward
        self.reward_matrix = np.reshape(
            self.reward, (self.num_states, self.num_actions), order="F"
        )
        # features matrix phi \in (SxA)xK where K is the number of features
        self.phi = phi
        self.phi_matrix = phi.reshape(self.num_states, self.num_actions, self.num_features, order='F')
        # phi_gail \in S X A X (SxA)
        self.phi_gail = self.compute_phi_gail()
        # Phi matrix for BC \in S x (AxK)
        # self.phi_SxAK = self.compute_phi_S_AK()

        # Stacked (I - gamma P_a)
        self.IGammaPAStacked = self.construct_design_matrix()
        # occupancy frequency of an expert's policy u[S x A]
        (u_E, opt_return) = self.solve_putterman_dual_LP_for_Opt_policy()
        self.u_E = u_E  # occupancy frequency of the expert's policy
        self.u_E_flat = u_E.reshape((self.num_states * self.num_actions), order="F")
        self.opt_policy = self.occupancy_freq_to_policy(u_E, True)
        self.opt_return = opt_return  # optimal return of the expert's policy
        (u_rand, rand_return) = self.generate_random_policy_return()
        self.random_return = rand_return
        self.u_rand = u_rand
        u_worst, u_worst_ret = self.solve_worst()
        self.worst_u = u_worst
        self.worst_return = u_worst_ret

    def compute_phi_gail(self) -> np.ndarray:
        """Compute phi_gail which is a matrix of size S x A x (SxA)
            Gail is really picky about the features..."""
        gail_features = self.num_states * self.num_actions
        phi_s = np.eye(self.num_states*self.num_actions)[range(self.num_states)]
        phi_gail = np.zeros((self.num_states, self.num_actions, self.num_actions*gail_features))
        for action in self.actions:
            phi_gail[:, action, range(action*gail_features, action*gail_features + gail_features)] = phi_s
        return phi_gail

    def compute_phi_S_AK(self) -> np.ndarray:
        """Compute phi_S_AK which is a matrix of size S x (A x K)
        This is used for the behavioral cloning solution"""
        phi_S_AK = np.zeros((self.num_states, (self.num_actions * self.num_features)))
        for s in self.states:
            for a in self.actions:
                s_prime = self.argmax_next_state(s, a)
                phi_S_AK[s, a*self.num_features:(a+1)*self.num_features] = self.phi_matrix[s_prime, a, :]
        return phi_S_AK
    
    def argmax_next_state(self, state: int, action: int) -> int:
        """Given state and action pair, return the most likely next state based on the MDP dynamics"""
        return int(np.argmax(self.P[state, :, action]))

    def next_state(self, state: int, action: int) -> int:
        """Given state and action pair, return the next state based on the MDP dynamics"""
        return np.random.choice(self.states, p=self.P[state, :, action])

    def occupancy_freq_to_policy(self, u, force_deterministic: bool = False) -> np.ndarray:
        """
        Converts u which is an occupancy frequency matrix of size S x A to a policy of size S x A
        Optionally, force the policy to be deterministic, by default it is stochastic
        """
        S = self.num_states
        A = self.num_actions
        policy = np.zeros((S, A), dtype=float)
        sum_u_s = np.sum(u, axis=1) # axis=1 means sum over rows
        for s in range(S):
            if force_deterministic:
                policy[s, np.argmax(u[s, :])] = 1.0
            else:
                if sum_u_s[s] > 1.0e-10:
                    policy[s, :] = u[s, :] / max(sum_u_s[s], 1.0e-10)
                else: # if the sum is 0, then we set it to random
                    rand_A_vec = np.random.rand(A)
                    policy[s] = rand_A_vec / np.sum(rand_A_vec)
        return policy

    def generate_samples_from_policy(
            self, episodes: int, horizon: int, policy: np.ndarray
    ) -> List[List[Tuple[int, int]]]:
        """Generate samples from the given policy
        policy = policy should be an SxA matrix where each row sums to 1
        """
        D = []  # Dataset of (s, a) pairs
        cur_state = np.random.choice(self.states, p=self.p_0)
        for _ in range(episodes):
            d = []
            for _ in range(horizon):
                action = np.random.choice(self.actions, p=policy[cur_state, :])
                d.append((cur_state, action))
                cur_state = self.next_state(cur_state, action)
            D.append(d)
        return D

    def generate_random_policy_return(self) -> Tuple[np.ndarray, float]:
        """Generate the return of a uniformly random policy where pi(a|s) = 1/|A|"""
        # Calculate P_pi for randomized pi
        P_pi = np.sum(self.P, axis=2) / self.num_actions
        r_pi = np.sum(self.reward_matrix, axis=1) / self.num_actions
        d_pi = np.linalg.inv(np.eye(self.num_states) - self.gamma * P_pi.T) @ self.p_0
        u_rand = np.zeros((self.num_states, self.num_actions))
        for s in range(self.num_states):
            for a in range(self.num_actions):
                u_rand[s, a] = d_pi[s] * 1/self.num_actions
        return u_rand, d_pi @ r_pi

    def generate_samples_from_occ_freq(
        self, num_samples, occupancy_freq
    ) -> List[Tuple[int, int]]:
        """
        Generate samples from the given occupancy frequency
        occupancy_freq = matrix of size S x A
        num_samples = number of samples to collect
        """
        return self.generate_samples_from_policy(1,
            num_samples, self.occupancy_freq_to_policy(occupancy_freq)
        )[0]

    def generate_expert_demonstrations(self, num_samples) -> List[Tuple[int, int]]:
        """A wrapper around generate_samples which calls it with the optimal occupancy_freq calculated by
        the dual putterman solution"""
        return self.generate_samples_from_policy(1,num_samples, self.opt_policy)[0]

    def generate_samples_from_action_policy(self, horizon, action_policy, behavior_policy) -> List[Tuple[int, int]]:
        """Generate samples from the given action policy, where transition dynamics are governed by the behavior policy"""
        D: List[Tuple[int, int]] = []
        cur_state = np.random.choice(self.states, p=self.p_0)
        for _ in range(horizon):
            D.append((cur_state, np.random.choice(self.actions, p=action_policy[cur_state, :])))
            cur_state = self.next_state(cur_state, np.random.choice(self.actions, p=behavior_policy[cur_state, :]))
        return D

    def generate_off_policy_demonstrations(self, episodes, horizon, behavior_occ_freq) -> List[List[Tuple[int, int]]]:
        """Generate demonstrations from the expert, using the behavior policy to govern the transition dynamics"""
        behavior_policy = self.occupancy_freq_to_policy(behavior_occ_freq)
        action_policy = self.opt_policy
        D: List[List[Tuple[int, int]]] = []
        for _ in range(episodes):
            D.append(self.generate_samples_from_action_policy(horizon, action_policy, behavior_policy))
        return D

    def generate_all_expert_demonstrations(self) -> List[Tuple[int, int]]:
        """Returns a list of all s,a pairs that the expert follows"""
        D = []
        for s in self.states:
            a = np.argmax(self.opt_policy[s, :])
            D.append((s, a))
        return D

    class SampleCollector:
        """This class is soley for the pickling required by the multiprocessing pool
        it remembers the occ_freq for the following call to generate_samples"""

        def __init__(self, mdp, policy, horizon):
            self.policy = policy
            self.mdp: MDP = mdp
            self.horizon = horizon

        def __call__(self, _) -> List[List[Tuple[int, int]]]:
            return self.mdp.generate_samples_from_policy(1,
                self.horizon, policy=self.policy
            )

    def generate_demonstrations_from_occ_freq(
        self, occ_freq, episodes=1, horizon=10, num_samples=None
    ) -> List[List[Tuple[int, int]]]:
        """Generate demonstrations from an occ freq
        Args:
            occ_freq: The occupancy frequency to use
            episodes: The number of episodes to generate
            horizon: The horizon of each episode
            num_samples: The number of samples to generate. If None, then episodes and horizon are used
        Returns:
            A list of episodes, where each episode is a list of (s,a) pairs
        """
        if num_samples:
            return [self.generate_samples_from_occ_freq(num_samples, occ_freq)]
        policy = self.occupancy_freq_to_policy(occ_freq)
        return self.generate_samples_from_policy(episodes, horizon, policy)

    def construct_design_matrix(self) -> np.ndarray:
        """
        Construct the design matrix consisting of (I - gamma P_a) stacked on top of eachother
        Returns an (SA X S) matrix
        """
        arrays = []
        I = np.eye(self.num_states)
        for action in self.actions:
            arrays.append((I - self.gamma * self.P[:,:,action]))
        return np.vstack(arrays)

    def solve_putterman_dual_LP_for_Opt_policy(self) -> Tuple[np.ndarray, float]:
        """This method solves the problem of Bellman Flow Constraint. This
        problem is sometimes called the dual problem of min p0^T v, which finds
        the optimal value function.

        Returns:
            ndarray: The optimal policy
            float: optimal return
        """
        method = "Dual_LP"
        a = self.num_actions
        s = self.num_states
        p_0 = self.p_0
        gamma = self.gamma
        r = self.reward
        W = self.IGammaPAStacked

        # Model
        model = gp.Model(method)
        model.Params.OutputFlag = 0
        # Variables
        u = model.addMVar(shape=(s * a), lb=0.0)
        # Constraints
        model.addMConstr(W.T, u, "==", p_0)
        # setting the objective
        model.setObjective(r @ u, GRB.MAXIMIZE)
        # Solve
        model.optimize()
        if model.Status != GRB.Status.OPTIMAL:
            raise ValueError("DUAL LP DID NOT FIND OPTIMAL SOLUTION")
        dual_return = model.objVal

        u_flat = u.X  # Gurobi way of getting the value of a model variable
        # Check to make sure u is an occupancy frequency
        # Return the occ_freq and the opt return
        for i in range(s * a): # make sure u is non-negative
            if u_flat[i] < 0:
                # print(u_flat[i])
               u_flat[i] = 0.0
        assert np.sum(u_flat) - (1 / (1 - gamma)) < 10**-2
        return u_flat.reshape((s, a), order="F"), dual_return

    def observed(self, state, D: Set[Tuple]) -> Tuple[bool, int]:
        for s, a in D:
            if state == s:
                return (True, a)
        return (False, -1)

    def construct_constraint_vector(self, D: Set[Tuple]) -> np.ndarray:
        """Constructs the constraint vector for u in Upsilon
        Returns a vector of length num_states * num_actions"""
        c = np.zeros((self.num_states, self.num_actions))
        for state in self.states:
            (observed_state, observed_action) = self.observed(state, D)
            for action in self.actions:
                # In order to be in Upsilon, you must observe the state with that action
                # Consistent with the expert
                if observed_state and action != observed_action:
                    # a constraint of 1 means that you shouldnt choose that (s,a) pair
                    c[state, action] = 1
        return c.reshape((self.num_states * self.num_actions), order="F")

    def solve_chebyshev_center(
        self, D: List[List[Tuple[int, int]]]
    ) -> Tuple[np.ndarray, float, float]:
        """Solves the chebyshev center problem to find the optimal occupancy_freq, more details in the paper
        Returns an SA vector u in U, the chebyshev radius, and the optimal return"""
        method = "Chebyshev"
        s = self.num_states
        a = self.num_actions
        phi = self.phi
        p_0 = self.p_0
        D_flat = set(itertools.chain.from_iterable(D))
        # print(
        #     f"len(D_flat) = {len(D_flat)} len(D) = {len(list(itertools.chain.from_iterable(D)))}"
        # )
        c = self.construct_constraint_vector(D_flat)
        W = self.IGammaPAStacked  # (I-\gamma P_a) stacked vertically

        model = gp.Model(method)
        model.Params.OutputFlag = 0

        # Define model variables
        sigma = model.addVar(lb=0.0, obj=1.0)
        u = model.addMVar(shape=(s * a), lb=0.0)
        alpha = model.addMVar(
            shape=(self.num_features), lb=-GRB.INFINITY, ub=GRB.INFINITY
        )
        alphaHat = model.addMVar(
            shape=(self.num_features),
            lb=-GRB.INFINITY,
            ub=GRB.INFINITY,
        )
        beta = model.addMVar(
            shape=(s, self.num_features), lb=-GRB.INFINITY, ub=GRB.INFINITY
        )
        betaHat = model.addMVar(
            shape=(s, self.num_features),
            lb=-GRB.INFINITY,
            ub=GRB.INFINITY,
        )

        # Add constraints for features
        for i in range(self.num_features):
            model.addConstr(
                (p_0 @ beta[:, i]) <= sigma + (phi[:, i] @ u)
            )
            model.addConstr(
                (p_0 @ betaHat[:, i]) <= sigma - (phi[:, i] @ u)
            )
            model.addConstr(phi[:, i] <= alpha[i] * c + (W @ beta[:, i]))
            model.addConstr(
                -1 * phi[:, i] <= alphaHat[i] * c + (W @ betaHat[:, i])
            )

        # Add constraints for u \in U
        model.addMConstr(W.T, u, "==", p_0)
        model.addConstr(c.T@ u == 0) # Constrain u to lie within Upsilon

        # setting the objective
        model.setObjective(sigma, GRB.MINIMIZE)

        # model.write("./" + method + ".lp") # write the model to a file, for debugging
        # Solve
        model.optimize()
        if model.Status != GRB.Status.OPTIMAL:
            raise ValueError(f"{method} DID NOT FIND OPTIMAL SOLUTION")
        radius = model.objVal
        u_flat = u.X  # Gurobi way of getting the value of a model variable
        # Check to make sure u is an occupancy frequency
        # assert np.sum(u_flat) - 1 / (1 - self.gamma) < 10**-2
        return u_flat.reshape((s, a), order="F"), radius, u_flat.T @ self.reward

    def worst_case_regret(self, D, u: np.ndarray) -> float:
        """Computes the worst case regret of the given occupancy frequency
        that is max pi_e max r (ro(pi_e, r) - ro(pi, r))
        """
        method = "Worst_Case_Regret"
        k = self.num_features
        W = self.IGammaPAStacked
        w_i_mat = np.zeros((k*2, k)) # each row is an extreme point of L1 norm ball
        for i in range(0, k):
            w_i_mat[i][i] = 1
            w_i_mat[i+k][i] = -1
        # Solve inner maximization
        max_v_r_i = np.zeros((k*2))
        model = gp.Model("inner maximization worst_case_regret")
        model.Params.OutputFlag = 0
        # Define model variables
        v = model.addMVar(shape=(self.num_states * self.num_actions), lb=0.0)
        model.addMConstr(W.T, v, "==", self.p_0)
        c = self.construct_constraint_vector(set(itertools.chain.from_iterable(D)))
        model.addConstr(c @ v == 0)
        # dont add linear constraint to worst case expert and reward
        for i in range(0, k*2):
            model.reset(0)
            model.setObjective(v@self.phi@w_i_mat[i], GRB.MAXIMIZE)
            model.optimize()
            if model.Status != GRB.Status.OPTIMAL:
                raise ValueError(f"{method} DID NOT FIND OPTIMAL SOLUTION")
            max_v_r_i[i] = model.objVal
        # compute regret
        return np.max(max_v_r_i - u@self.phi@w_i_mat.T)

    def solve_cheb_part_2(self, D: List[List[Tuple[int, int]]], add_lin_constr: bool, add_linf_constr: bool, passed_eps = None, prune = False) -> Tuple[float, np.ndarray, float, float]:
        """Solves the chebyshev center problem to find the optimal occupancy_freq, this version first solves 
        an inner maximization problem, then an outer minimization problem
        Returns eps used for constraints, SA matrix u in U, the chebyshev radius, and the optimal return"""
        method = "Chebyshev_part_2"
        s = self.num_states
        a = self.num_actions
        phi = self.phi
        k = self.num_features
        sa = s * a
        p_0 = self.p_0
        D_flat = set(itertools.chain.from_iterable(D))
        c = self.construct_constraint_vector(D_flat)
        W = self.IGammaPAStacked
        u_e_hat = self.u_hat_all(D).reshape((sa), order="F")
        sampled_points = k*2
        threshold = -np.inf
        if prune:
            sampled_points = 100
            # Generate the sample points
            w_i_mat = np.zeros((sampled_points, k)) # each row is a sampled point of R l1
            for i in range(0, sampled_points):
                w = np.random.rand(k) * 2 - 1
                while np.linalg.norm(w, ord=1) > 1:
                    w = np.abs(np.random.rand(k) * 2 - 1)
                w_i_mat[i] = w / np.linalg.norm(w, ord=1)
            threshold = np.quantile(u_e_hat @ phi @ w_i_mat.T, 0.9)
        else:
            w_i_mat = np.zeros((k*2, k))
            for i in range(0, k):
                w_i_mat[i][i] = 1
                w_i_mat[i+k][i] = -1

        # solve inner maximization problem
        max_v_r_i = np.zeros((sampled_points))
        model = gp.Model("inner maximization")
        model.Params.OutputFlag = 0
        model.Params.LogToConsole = 0
        # Define model variables
        v = model.addMVar(shape=(sa), lb=0.0)
        model.addMConstr(W.T, v, "==", p_0)
        if add_lin_constr:
            model.addConstr(c @ v == 0)
        # eps = 5000
        # Use the true epsilon if passsed no epsilon
        eps = passed_eps if passed_eps else (np.linalg.norm(((self.u_E).reshape((sa), order="F") - self.u_hat_all(D).reshape((sa), order="F"))@phi, ord=np.inf) + 1)
        if add_linf_constr:
            model.addConstr(v @ phi - u_e_hat @ phi <= eps)
            model.addConstr(-v @ phi + u_e_hat @ phi <= eps)
        for i in range(0, sampled_points): # for each extreme point of R
            model.reset(0)
            if prune: # prune the extreme points that dont do well with u_e_hat
                if (u_e_hat @ phi @ w_i_mat[i]) < threshold:
                    max_v_r_i[i] = -np.inf
                    continue
            model.setObjective(v@phi@w_i_mat[i], GRB.MAXIMIZE)
            model.optimize()
            if model.Status != GRB.Status.OPTIMAL:
                raise ValueError(f"{method} DID NOT FIND OPTIMAL SOLUTION")
            max_v_r_i[i] = model.objVal
        # solve outer minimization problem
        model = gp.Model("outer minimization")
        model.Params.OutputFlag = 0
        model.Params.LogToConsole = 0
        # Define model Variables
        sigma = model.addVar(lb=0.0, obj=1.0)
        u = model.addMVar(shape=(sa), lb=0.0)
        model.addConstr(-u@phi@w_i_mat.T + max_v_r_i <= sigma)
        model.addMConstr(W.T, u, "==", p_0)
        model.addConstr(c @ u == 0) # INFO: CONSTRAINT U TO BE IN UPSILON 
        model.setObjective(sigma, GRB.MINIMIZE)
        model.optimize()
        if model.Status != GRB.Status.OPTIMAL:
            raise ValueError(f"{method} DID NOT FIND OPTIMAL SOLUTION")
        radius = model.objVal
        u_flat = u.X
        assert(np.sum(u_flat) - (1 / (1 - self.gamma)) < 10**-2)
        return float(eps), u_flat.reshape((s, a), order="F"), radius, u_flat.T @ self.reward

    def compute_V_hat(self, D: List[List[Tuple[int, int]]], u_e_hat=None) -> np.ndarray:
        phi = self.phi
        if u_e_hat is None:
            #u_e_hat is an SA vector
            u_e_hat = self.u_hat_all(D).reshape((self.num_states * self.num_actions), order="F")
        return u_e_hat @ phi

    def solve_syed(self, D: List[List[Tuple[int, int]]], episodes: int, horizon: int, u_e_hat = None, add_lin_constr = False) -> Tuple[np.ndarray, float, float]:
        """Solve the LPAL LP from Syed.
        Args:
            D: list of trajectories (which themselves are lists of length horizon)
            episodes: number of episodes
            horizon: horizon of each episode
        Returns:
            a occupancy_freq matrix, the radius of the LPAL set, and the return achieved.
        """
        method = "Syed LPAL"
        s = self.num_states
        a = self.num_actions
        p_0 = self.p_0
        phi = self.phi
        W = self.IGammaPAStacked
        # Using the experts sample trajectories D, compute an epsilon-good estimate of V
        V_hat = self.compute_V_hat(D, u_e_hat)
        # Solve the LPAL formulation
        model = gp.Model(method)
        model.Params.OutputFlag = 0

        u = model.addMVar(shape=(self.num_states * self.num_actions), name="u", lb=0.0)
        B = model.addVar(name="B", lb=0.0)
        # model.addConstr(B <= (u@phi) - V_hat)
        model.addConstr(B >= (u@phi) - V_hat)
        model.addConstr(B >= (-1 *(u@phi)) + V_hat)
        model.addMConstr(W.T, u, "==", p_0)
        if add_lin_constr:
            c = self.construct_constraint_vector(set(itertools.chain.from_iterable(D)))
            model.addConstr(c @ u == 0)

        # model.write("./" + method + ".lp") # write the model to a file, for debugging
        model.setObjective(B, GRB.MINIMIZE)

        # Solve
        model.optimize()
        if model.Status != GRB.Status.OPTIMAL:
            raise ValueError(f"{method} DID NOT FIND OPTIMAL SOLUTION")

        u_flat = u.X  # Gurobi way of getting the value of a model variable
        radius = model.objVal
        # Check to make sure u is an occupancy frequency
        # assert np.sum(u_flat) - 1 / (1 - self.gamma) < 10**-2
        return u_flat.reshape((s, a), order="F"), radius, u_flat @ self.reward

    def sigmoid_policy(self, theta: np.ndarray, state: int) -> np.ndarray:
        """Compute the sigmoid policy for a given state and parameter vector theta
        theta = parameter vector of size K
        returns a vector of size A"""
        return scipy.special.softmax(self.phi_gail[state,:] @ theta)

    def grad_log_policy(self, theta: np.ndarray, state: int, action: int) -> np.ndarray:
        """Compute the gradient of the log policy for a given state, action, and parameter vector theta
        theta = parameter vector of size K
        returns a vector of size K"""
        return self.phi_gail[state,action] - self.phi_gail[state,:].T @ self.sigmoid_policy(theta, state)
    
    def occ_freq_from_P_pi(self, P_pi: np.ndarray, pi) -> np.ndarray:
        """Compute the matrix U
        given a matrix P_pi of size SxS
        and a function pi that maps states to a simplex over actions
        returns a matrix of size SxA
        """
        u = np.zeros((self.num_states, self.num_actions))
        dtheta = np.linalg.inv(np.eye(self.num_states) - self.gamma * P_pi.T) @ self.p_0
        for s in self.states:
            u[s,:] = pi(s) * dtheta[s]
        return u

    def P_pi(self, pi) -> np.ndarray:
        """Compute the matrix P_pi
        given a function pi that maps states to a simplex over actions
        returns a matrix of size SxS"""
        Ptheta = np.zeros((self.num_states, self.num_states))
        for s in self.states:
            for s_prime in self.states:
                Ptheta[s,s_prime] = pi(s) @ self.P[s,s_prime,:]
        return Ptheta

    def u_theta_matrix(self, theta: np.ndarray) -> np.ndarray:
        """Compute the matrix U_theta
        theta = parameter vector of size K
        returns a matrix of size SxA"""
        pi = lambda s: self.sigmoid_policy(theta, s)
        Ptheta = self.P_pi(pi)
        return self.occ_freq_from_P_pi(Ptheta, pi)
    
    def generate_samples_from_sigmoid_policy(self, theta: np.ndarray, episodes: int, horizon: int) -> List[List[Tuple[int, int]]]:
        """Generate samples from the sigmoid policy
        theta = parameter vector of size K
        returns a list of episodes, each of length horizon"""
        D: List[List[Tuple[int, int]]] = []
        for _ in range(0, episodes):
            d = []
            cur_state = np.random.choice(self.states, p=self.p_0)
            for _ in range(0, horizon):
                action = np.random.choice(self.actions, p=self.sigmoid_policy(theta, cur_state))
                d.append((cur_state, action))
                cur_state = self.next_state(cur_state, action)
            D.append(d)
        return D

    def u_hat_all(self, D:List[List[Tuple[int,int]]]) -> np.ndarray:
        """Compute the empirical occupancy frequency of a given state-action pair
        returns a matrix of all occupancy_freq (SxA)"""
        u_hat = np.zeros((self.num_states, self.num_actions))
        for d in D:
            for t, (s,a) in enumerate(d):
                u_hat[s,a] += self.gamma**t
        return u_hat / len(D)

    def D_w(self, state: int, action: int, w: np.ndarray) -> float:
        """Compute the sigmoid function for the current state, action
            returns a float"""
        # return 1 / (1 + np.exp(-1 * self.phi_gail[state,action] @ w))
        return scipy.special.expit(self.phi_gail[state,action] @ w)

    def Q(self, w:np.ndarray, u_theta:np.ndarray, D:List[List[Tuple[int,int]]]) -> float:
        """Compute the Q value for a given parameter vector w and dataset D
        returns a float"""
        count = 0
        for d in D:
            for (s,a) in d:
                count += u_theta[s,a] * scipy.special.expit(self.phi_gail[s,a] @ w)
        return count / len(D)

    def Q_log(self, theta:np.ndarray, D:List[List[Tuple[int,int]]]) -> float:
        """Compute the Q value for a given parameter vector theta and dataset d
        returns a float"""
        count = 0
        for d in D:
            for (s,a) in d:
                count -= np.log(self.sigmoid_policy(theta, s)[a])
        return count / len(D)

    def grad_H(self, theta: np.ndarray, u_hat: np.ndarray, D: List[List[Tuple[int,int]]]) -> np.ndarray:
        """Compute the H value for a given parameter vector theta
        returns a float"""
        gail_features = self.num_states * self.num_actions
        grad_h = np.zeros(self.num_actions*gail_features)
        Q = self.Q_log(theta, D)
        for d in D:
            for (s,a) in d:
                q = u_hat[s,a] * (Q - np.log(self.sigmoid_policy(theta, s)[a]))
                grad_h += u_hat[s,a] * q * self.grad_log_policy(theta, s, a)
        return grad_h

    def obj(self, theta: np.ndarray) -> float:
        """Compute the objective function for a given parameter vector theta
        returns a float"""
        ue = np.sum(self.u_E, axis=1)
        utheta = np.sum(self.u_theta_matrix(theta), axis=1)
        count = scipy.stats.entropy(utheta, (ue + utheta) / 2) 
        count += scipy.stats.entropy(ue, (ue + utheta) / 2)
        return count

    def verify_theta_return(self, theta: np.ndarray) -> float:
        """Compute the return of a given parameter vector theta
        theta : vector of a parameterized policy
        returns a float"""
        utheta = self.u_theta_matrix(theta) # Solves for the occupancy frequency from a linear system of eqs
        ret = 0.0
        for s in self.states:
            for a in self.actions:
                ret += utheta[s,a] * self.reward_matrix[s,a]
        return ret

    def solve_GAIL(self, D_e: List[List[Tuple[int, int]]], episodes: int, horizon: int) -> tuple[float, np.ndarray]:
        """Solve the GAIL formulation"""
        phi = self.phi_gail
        gail_features = self.num_states * self.num_actions
        learning_rate = 10000
        E_d_w_expert = np.zeros(self.num_actions*gail_features)
        E_d_w_theta = np.zeros(self.num_actions*gail_features)
        grad_theta = np.zeros(self.num_actions*gail_features)
        # theta_cur = np.random.rand(self.num_actions*gail_features)
        theta_cur = np.ones(self.num_actions*gail_features)
        # w_cur = np.random.rand(self.num_actions*gail_features)
        w_cur = np.ones(self.num_actions*gail_features)
        u_e = self.u_hat_all(D_e)
        # u_e = self.u_E
        # prior_obj = self.obj(theta_cur)
        # while(prior_obj > 0.001 and iteration < 10):
        for _ in range(20):
            D_theta = self.generate_samples_from_sigmoid_policy(theta_cur, 1, horizon)
            u_theta = self.u_hat_all(D_theta)
            # u_theta = self.u_theta_matrix(theta_cur)
            # Reset the gradients
            E_d_w_expert *= 0.0
            E_d_w_theta *= 0.0
            grad_theta *= 0.0

            # for s in self.states:
                # for a in self.actions:
            for tau_theta in D_theta:
                for (s,a) in tau_theta:
                    E_d_w_theta += u_theta[s,a] * (1 - self.D_w(s,a,w_cur)) * phi[s,a,:]
            # for s_e in self.states:
                # for a_e in self.actions:
            for tau_exp in D_e:
                for (s_e,a_e) in tau_exp:
                    E_d_w_expert += u_e[s_e,a_e] * -1 * self.D_w(s_e,a_e,w_cur) * phi[s_e,a_e,:]
            grad_w = E_d_w_expert + E_d_w_theta
            w_cur += learning_rate * grad_w
            Q = self.Q(w_cur, u_theta, D_theta)
            # for s in self.states:
                # for a in self.actions:
            for tau_theta in D_theta:
                for (s,a) in tau_theta:
                    u_hat = u_theta[s,a]
                    q = Q + (u_hat * scipy.special.log_expit(phi[s,a,:] @ w_cur))
                    grad_log_policy = self.grad_log_policy(theta_cur, s, a)
                    grad_theta += u_hat * q * grad_log_policy
            # grad_H = self.grad_H(theta_cur, u_theta, D_theta)
            # grad_theta -= 0.30*grad_H
            theta_cur -= learning_rate * grad_theta
            # prior_obj = self.obj(theta_cur)
            # learning_rate *= 0.99
        return self.verify_theta_return(theta_cur), self.u_theta_matrix(theta_cur)  # This takes a long time to compute

    def solve_BC(self, D_e: List[List[Tuple[int,int]]], episodes:int, horizon:int) -> float:
        phi = self.phi_SxAK
        D_flat = set(itertools.chain.from_iterable(D_e))
        # D_flat = list(itertools.chain.from_iterable(D_e))
        model = sklearn.linear_model.LogisticRegression(multi_class='multinomial', solver='lbfgs', max_iter=5000)
        X = np.zeros((len(D_flat), self.num_actions*self.num_features))
        y = np.zeros(len(D_flat))
        observed_actions = {0:0, 1:0, 2:0, 3:0}
        for ind, (s,a) in enumerate(D_flat):
            X[ind, :] = phi[s,:]
            y[ind] = a
            observed_actions[a] = 1
        try:
            model.fit(X, y)
        except ValueError: # This happens when there is only one class in the dataset
            return self.random_return
        pi_mat = model.predict_proba(phi)
        # Pad the pi_mat since we may not have all actions in the dataset
        if sum(observed_actions.values()) < self.num_actions:
            for a in range(self.num_actions):
                if not observed_actions[a]:
                    pi_mat = np.insert(pi_mat, a, 0, axis=1)
        pi = lambda s: pi_mat[s,:] 
        u_bc = self.occ_freq_from_P_pi(self.P_pi(pi), pi)
        return u_bc.reshape(self.num_states*self.num_actions, order="F") @ self.reward

    def solve_naive_BC(self, D_e: List[List[Tuple[int, int]]], episodes: int, horizon: int) -> Tuple[np.ndarray, float]:
        """Solve the occupancy frequency cloning formulation
        returns a float"""
        u_e = self.u_hat_all(D_e)
        pi_mat = self.occupancy_freq_to_policy(u_e)
        pi = lambda s: pi_mat[s,:]
        u = self.occ_freq_from_P_pi(self.P_pi(pi), pi)
        return u, u.reshape(self.num_states*self.num_actions, order="F") @ self.reward

    def solve_worst(self) -> Tuple[np.ndarray, float]:
        """Solve the worst-case bellman flow problem
        returns a tuple of the worst-case occupancy frequency and the 
        corresponding worst-case return"""
        method = "Worst"
        a = self.num_actions
        s = self.num_states
        p_0 = self.p_0
        r = self.reward
        W = self.IGammaPAStacked

        # Model
        model = gp.Model(method)
        model.Params.OutputFlag = 0
        # Variables
        u = model.addMVar(shape=(s * a), lb=0.0)
        # Constraints
        model.addMConstr(W.T, u, "==", p_0)
        # setting the objective
        model.setObjective(r @ u, GRB.MINIMIZE)
        # Solve
        model.optimize()
        if model.Status != GRB.Status.OPTIMAL:
            raise ValueError("MIN DUAL LP DID NOT FIND OPTIMAL SOLUTION")
        dual_return = model.objVal

        u_flat = u.X  # Gurobi way of getting the value of a model variable
        # Check to make sure u is an occupancy frequency
        # assert np.sum(u_flat) - 1 / (1 - gamma) < 10**-2
        # Return the occ_freq and the opt return
        return u_flat.reshape((s, a), order="F"), dual_return

    def solve_MILO(self):
        pass

# INFO: For testing purposes only
if __name__ == "__main__":
    print("------------------------------Running Gridworlds Main Func from rmdp.py------------------------------")
    import gridworld
    gridworld.main()
