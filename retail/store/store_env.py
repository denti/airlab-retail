from collections import namedtuple
from math import pi

import torch
import torch.distributions as d
import torch.nn.functional as F

from rlpyt.envs.base import Env, EnvStep
from rlpyt.spaces.int_box import IntBox
from rlpyt.spaces.float_box import FloatBox
from rlpyt.utils.quick_args import save__init__args

from retail.utility import LinearUtility, LogLinearUtility, CobbDouglasUtility, HomogeneousReward

from .assortment import Assortment


EnvInfo = namedtuple('EnvInfo',
                     ['sales', 'availability', 'waste', 'reward', 'traj_done'])


class StoreEnv(Env):

    def __init__(
        self,
        assortment_size=1000,  # number of items to train
        max_stock=1000,  # Size of maximum stock
        clip_reward=False,
        episodic_lives=True,
        repeat_action_probability=0.0,
        horizon=10000,
        seed=None,
        substep_count=4,
        bucket_customers=torch.tensor([800., 400., 500., 900.]),
        bucket_cov=torch.eye(4) / 100,
        forecastBias=0.0,
        forecastVariance=0.0,
        freshness=1,
        utility_function='homogeneous',
        utility_weights={'alpha': 1., 'beta': 1., 'gamma': 1.},
        characDim=4,
        lead_time=1,  # Defines how quickly the orders goes through the buffer - also impacts the relevance of the observation
        lead_time_fast=0,
        symmetric_action_space=False,
    ):
        save__init__args(locals(), underscore=True)

        self.bucket_customers = bucket_customers
        self.assortment_size = assortment_size
        self.freshness = freshness
        self.seed = seed
        self.max_stock = max_stock
        self.utility_function = utility_function
        self.utility_weights = {
            'alpha': utility_weights['alpha'],
            'beta': utility_weights['beta'],
            'gamma': utility_weights['gamma'],
        }
        self.horizon = horizon
        self.lead_time = lead_time
        self.lead_time_fast = lead_time_fast
        self.forecastBias = forecastBias
        self.forecastVariance = forecastVariance
        self.substep_count = substep_count
        self.bucket_cov = bucket_cov

        # Spaces

        if symmetric_action_space:
            self._action_space = FloatBox(low=-max_stock / 2,
                                          high=max_stock / 2, shape=[assortment_size])
        else:
            self._action_space = IntBox(low=0, high=max_stock,
                                        shape=[assortment_size])
        self.stock = torch.zeros(assortment_size, max_stock,
                                 requires_grad=False)

        # correct high with max shelf life

        self._observation_space = FloatBox(low=0, high=1000,
                                           shape=(assortment_size, max_stock + characDim
                                                  + lead_time + lead_time_fast + 1))
        self._horizon = int(horizon)
        self.assortment = Assortment(assortment_size, freshness, seed)
        self._repeater = torch.stack((self.assortment.shelf_lives,
                                      torch.zeros(self._assortment_size))).transpose(0,
                                                                                     1).reshape(-1).detach()
        self.forecast = torch.zeros(assortment_size, 1)  # DAH forecast.
        self._step_counter = 0

        # Needs to move towards env parameters

        self._customers = \
            d.multivariate_normal.MultivariateNormal(bucket_customers,
                                                     bucket_cov)
        self.assortment.base_demand = \
            self.assortment.base_demand.detach() \
            / bucket_customers.sum()
        self._bias = d.normal.Normal(forecastBias, forecastVariance)

        # We want a yearly seasonality - We have a cosinus argument and a phase.
        # Note that, as we take the absolute value, 2*pi/365 becomes pi/365.

        self._year_multiplier = torch.arange(0.0, horizon, pi / 365)
        self._week_multiplier = torch.arange(0.0, horizon, pi / 7)
        self._phase = 2 * pi * torch.rand(assortment_size)
        self._phase2 = 2 * pi * torch.rand(assortment_size)
        self.create_buffers(lead_time, lead_time_fast)
        if utility_function == 'linear':
            self.utility_function = LinearUtility(**utility_weights)
        elif utility_function == 'loglinear':
            self.utility_function = LogLinearUtility(**utility_weights)
        elif utility_function == 'cobbdouglas':
            self.utility_function = CobbDouglasUtility(**utility_weights)
        elif utility_function == 'homogeneous':
            self.utility_function = HomogeneousReward(**utility_weights)
        else:
            self.utility_function = utility_function
        self._updateEnv()
        for i in range(self._lead_time):
            self._addStock((self.forecast.squeeze()
                            * bucket_customers[i]).round())

    def reset(self):
        self._updateObs()
        self._step_counter = 0
        return self.get_obs()

    def step(self, action):
        if self._symmetric_action_space:
            new_action = torch.as_tensor(action.round().clip(0,
                                                             self._max_stock) + self._max_stock / 2,
                                         dtype=torch.int32)
        else:
            new_action = torch.as_tensor(action,
                                         dtype=torch.int32).clamp(0, self._max_stock)
        if self.day_position % self._substep_count == 0:
            order_cost = self._make_fast_order(new_action)
            (sales, availability) = \
                self._generateDemand(self.real.clamp_(0.0, 1.))
            waste = self._waste()  # Update waste and store result
            self._reduceShelfLives()
            self._step_counter += 1
            self._updateEnv()
        else:

            self.day_position += 1
            order_cost = self._make_order(new_action)
            (sales, availability) = \
                self._generateDemand(self.real.clamp_(0.0, 1.))
            waste = 0  # By default, no waste before the end of day
            self._updateObs()
        sales.sub_(order_cost)
        utility = self.utility_function.reward(sales, waste, availability)
        done = self._step_counter == self.horizon
        info = EnvInfo(sales=sales, availability=availability,
                       waste=waste, reward=utility, traj_done=done)
        return EnvStep(self.get_obs(), utility, done, info)

    def get_obs(self):
        return self._obs

    def to_json(self):
        return {
            'bucket_customers': self.bucket_customers,
            'assortment_size': self.assortment_size,
            'freshness': self.freshness,
            'seed': self.seed,
            'max_stock': self.max_stock,
            'utility_function': self.utility_function,
            'utility_weights': {
                'alpha': self.utility_weights['alpha'],
                'beta': self.utility_weights['beta'],
                'gamma': self.utility_weights['gamma'],
            },
            'horizon': self.horizon,
            'lead_time': self.lead_time,
            'lead_time_fast': self.lead_time_fast,
            'forecastBias': self.forecastBias,
            'forecastVariance': self.forecastVariance,
            'substep_count': self.substep_count,
            'bucket_cov': self.bucket_cov,
        }

    # ##########################################################################
    # Helpers

    def _updateObs(self):
        self._obs = torch.cat((self.stock, self.assortment.characs,
                               self.forecast, torch.stack(self._buffer
                                                          + self._buffer_fast, 1),
                               torch.ones(self._assortment_size, 1)
                               * self.day_position), 1)

    def _updateEnv(self):
        self.day_position = 1
        argument = self._year_multiplier[self._step_counter] \
            + self._phase
        argument2 = self._week_multiplier[self._step_counter] \
            + self._phase2
        self.forecast = (self.assortment.base_demand
                         * argument.cos().abs()
                         * argument2.cos().abs()).view(-1, 1)
        self.real = self.forecast.view(-1) \
            + self._bias.sample((self._assortment_size, ))
        self._updateObs()

    def _addStock(self, units):
        padding = self._max_stock - units
        replenishment = torch.stack((units, padding)).t().reshape(-1)
        restock_matrix = \
            self._repeater.repeat_interleave(repeats=replenishment.long(),
                                             dim=0).view(self._assortment_size, self._max_stock)
        torch.add(self.stock.sort(1)[0], restock_matrix.sort(1,
                                                             descending=True)[0], out=self.stock)
        total_units = \
            restock_matrix.ge(1).sum(1).add_(self.stock.ge(1).sum(1))
        penalty_cost_forbidden = F.relu(total_units
                                        - self._max_stock).float().mul_(self.assortment.selling_price)
        return penalty_cost_forbidden

    def _sellUnits(self, units):
        sold = torch.min(self.stock.ge(1).sum(1).float(), units)
        availability = \
            self.stock.ge(1).sum(1).float().div(units).clamp(0, 1)
        availability[torch.isnan(availability)] = 1.
        reward = \
            sold.mul_(2).sub_(units).mul(self.assortment.selling_price
                                         - self.assortment.cost)
        (p, n) = self.stock.shape
        stock_vector = self.stock.sort(1, descending=True)[0].view(-1)
        to_keep = n - units

        # IMPROVE INTERLEAVER SPEED ###

        interleaver = torch.stack((units, to_keep)).t().reshape(2,
                                                                p).view(-1).long()
        binary_vec = torch.tensor([0.0,
                                   1]).repeat(p).repeat_interleave(interleaver)
        self.stock = binary_vec.mul_(stock_vector).view(p, n)
        return (reward, availability)

    def _waste(self):
        waste = torch.mul(self.stock.eq(1).sum(1).float(),
                          self.assortment.selling_price)
        return waste

    def _reduceShelfLives(self):
        self.stock = F.relu(self.stock - 1)

    def _generateDemand(self, consumption_prob):
        sampled_customers = \
            self._customers.sample().round().int()[self.day_position
                                                   - 1]
        purchases_gen = d.bernoulli.Bernoulli(consumption_prob)
        demand = purchases_gen.sample((sampled_customers,
                                       )).sum(0).clamp(0, self._max_stock)
        (reward, availability) = self._sellUnits(demand)
        return (reward, availability)

    # Updates stock matrix and transportation cost (reward)
    # order speed increases the speed of all orders currently in the buffer.

    def _make_order(self, units):
        self._buffer.append(units.float().view(-1))
        penaltyCost = self._addStock(self._buffer.pop(0))
        return penaltyCost

    def _make_fast_order(self, units):
        self._buffer_fast.append(units.float().view(-1))
        penaltyCost = self._addStock(self._buffer_fast.pop(0))
        return penaltyCost

    def get_partial_position(self):
        return self.stock.ge(1).sum(1).float()

    def get_full_inventory_position(self):
        ip = self.get_partial_position()
        ip += torch.stack(self._buffer).sum(0)
        return ip

    def create_buffers(self, slow_speed, fast_speed):
        self._buffer = []
        self._buffer_fast = []
        for i in range(slow_speed):
            self._buffer.append(torch.zeros(self._assortment_size))
        for i in range(fast_speed):
            self._buffer_fast.append(torch.zeros(self._assortment_size))

    def transportation_cost(
        self,
        units,
        transport_size=300000,
        transport_cost=250.,
    ):
        volume = units * self.assortment.dims.t().sum(0)
        number_of_trucks = np.trunc(volume.sum() / transport_size) + 1

        # This +1 has no impact even if the order is 0 as we return the contribution to the total cost, not the total cost itself

        total_cost = number_of_trucks * transport_cost
        return volume / volume.sum() * total_cost

    # ##########################################################################
    # Properties

    @property
    def clip_reward(self):
        return self._clip_reward

    @property
    def horizon(self):
        return self._horizon
