# coding=utf-8
# Copyright 2020 The Trax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Classes for RL training in Trax."""

import functools
import os

import numpy as np
import tensorflow as tf

from trax import layers as tl
from trax import lr_schedules as lr
from trax import shapes
from trax import supervised
from trax.math import numpy as jnp
from trax.rl import advantages as rl_advantages
from trax.rl import training as rl_training


class ActorCriticTrainer(rl_training.PolicyTrainer):
  """Trains policy and value models using actor-critic methods.

  Attrs:
    on_policy (bool): Whether the algorithm is on-policy. Used in the data
      generators. Should be set in derived classes.
  """

  on_policy = None

  def __init__(self, task,
               value_model=None,
               value_optimizer=None,
               value_lr_schedule=lr.MultifactorSchedule,
               value_batch_size=64,
               value_train_steps_per_epoch=500,
               value_evals_per_epoch=1,
               value_eval_steps=1,
               n_shared_layers=0,
               added_policy_slice_length=0,
               n_replay_epochs=1,
               scale_value_targets=False,
               q_value=False,
               q_value_n_samples=1,
               **kwargs):  # Arguments of PolicyTrainer come here.
    """Configures the actor-critic Trainer.

    Args:
      task: RLTask instance to use
      value_model: the model to use for the value function
      value_optimizer: the optimizer to train the value model
      value_lr_schedule: lr schedule for value model training
      value_batch_size: batch size for value model training
      value_train_steps_per_epoch: how many steps are we using to
        train the value model in each epoch
      value_evals_per_epoch: number of value trainer evaluations per RL
          epoch - only affects metric reporting.
      value_eval_steps: number of value trainer steps per evaluation -
          only affects metric reporting.
      n_shared_layers: how many layers to share between value and
        policy models
      added_policy_slice_length: how much longer should slices of
        trajectories be for policy than for value training; this
        is useful for TD calculations and only affect the length
        of elements produced for policy batches; value batches
        have maximum length set by max_slice_length in **kwargs
     n_replay_epochs: how many last epochs to take into the replay buffer;
        only makes sense for off-policy algorithms
     scale_value_targets: whether to scale targets for the value function by
        1 / (1 - gamma)
     q_value: whether to use Q-values as baselines
     q_value_n_samples: number of samples to average over when calculating
        baselines based on Q-values
     **kwargs: arguments for PolicyTrainer super-class
    """
    self._n_shared_layers = n_shared_layers
    self._value_batch_size = value_batch_size
    self._value_train_steps_per_epoch = value_train_steps_per_epoch
    self._value_evals_per_epoch = value_evals_per_epoch
    self._value_eval_steps = value_eval_steps

    # The 2 below will be initalized in super.__init__ anyway, but are needed
    # to construct value batches which are needed before PolicyTrainer init
    # since policy input creation calls the value model -- hence this code.
    self._task = task
    self._max_slice_length = kwargs.get('max_slice_length', 1)
    self._added_policy_slice_length = added_policy_slice_length
    self._n_replay_epochs = n_replay_epochs
    task.set_n_replay_epochs(n_replay_epochs)

    if scale_value_targets:
      self._value_network_scale = 1 / (1 - self._task.gamma)
    else:
      self._value_network_scale = 1

    self._q_value = q_value
    self._q_value_n_samples = q_value_n_samples
    if q_value:
      value_model = functools.partial(value_model, inject_actions=True)
    self._value_eval_model = value_model(mode='eval')
    self._value_eval_model.init(self._value_model_signature)

    # Initialize policy training.
    super(ActorCriticTrainer, self).__init__(task, **kwargs)

    # Initialize training of the value function.
    value_output_dir = kwargs.get('output_dir', None)
    if value_output_dir is not None:
      value_output_dir = os.path.join(value_output_dir, 'value')
      # If needed, create value_output_dir and missing parent directories.
      if not tf.io.gfile.isdir(value_output_dir):
        tf.io.gfile.makedirs(value_output_dir)
    self._value_inputs = supervised.Inputs(
        train_stream=lambda _: self.value_batches_stream())
    self._value_trainer = supervised.Trainer(
        model=value_model,
        optimizer=value_optimizer,
        lr_schedule=value_lr_schedule,
        loss_fn=tl.L2Loss(),
        inputs=self._value_inputs,
        output_dir=value_output_dir,
        metrics={'value_loss': tl.L2Loss()})

  @property
  def _value_model_signature(self):
    obs_sig = shapes.signature(self._task.observation_space)
    target_sig = mask_sig = shapes.ShapeDtype(
        shape=(1, 1, 1),
    )
    inputs_sig = (obs_sig.replace(shape=(1, 1) + obs_sig.shape),)
    if self._q_value:
      act_sig = shapes.signature(self._task.action_space)
      inputs_sig += (act_sig.replace(shape=(1, 1) + act_sig.shape),)
    return (*inputs_sig, target_sig, mask_sig)

  @property
  def _replay_epochs(self):
    if self.on_policy:
      assert self._n_replay_epochs == 1, (
          'Non-unit replay buffer size only makes sense for off-policy '
          'algorithms.'
      )
    return [-(ep + 1) for ep in range(self._n_replay_epochs)]

  def _run_value_model(self, observations, dist_inputs):
    if dist_inputs is None:
      dist_inputs = jnp.zeros(
          observations.shape[:2] + (self._policy_dist.n_inputs,)
      )

    actions = None
    if self._q_value:
      dist_inputs = np.broadcast_to(
          dist_inputs, (self._q_value_n_samples,) + dist_inputs.shape
      )
      actions = self._policy_dist.sample(dist_inputs)
      inputs = (observations, actions)
    else:
      inputs = (observations,)

    values = self._value_eval_model(
        inputs, n_accelerators=1
    ) * self._value_network_scale
    values = np.squeeze(values, axis=-1)  # Remove the singleton depth dim.

    return (values, actions)

  def _aggregate_values(self, values):
    if self._q_value:
      # TODO(pkozakowski): Try max here.
      return jnp.mean(values, axis=0)
    else:
      return values

  def value_batches_stream(self):
    """Use the RLTask self._task to create inputs to the value model."""
    max_slice_length = self._max_slice_length + self._added_policy_slice_length
    for np_trajectory in self._task.trajectory_batch_stream(
        self._value_batch_size,
        max_slice_length=max_slice_length,
        min_slice_length=(1 + self._added_policy_slice_length),
        epochs=self._replay_epochs,
    ):
      (values, _) = self._run_value_model(
          np_trajectory.observations, np_trajectory.dist_inputs
      )
      values = self._aggregate_values(values)

      # TODO(pkozakowski): Add some shape assertions and docs.
      # Calculate targets based on the advantages over the target network - this
      # allows TD learning for value networks.
      advantages = self._advantage_estimator(
          np_trajectory.rewards, np_trajectory.returns, values,
          gamma=self._task.gamma,
          n_extra_steps=self._added_policy_slice_length,
      )
      length = advantages.shape[1]
      values = values[:, :length]
      target_returns = values + advantages

      inputs = (np_trajectory.observations[:, :length],)
      if self._q_value:
        inputs += (np_trajectory.actions[:, :length],)

      # Insert an extra depth dimension, so the target shape is consistent with
      # the network output shape.
      yield (
          # Inputs: observations and maybe actions.
          *inputs,
          # Targets: computed returns.
          target_returns[:, :, None] / self._value_network_scale,
          # Mask to zero-out padding.
          np_trajectory.mask[:, :length, None],
      )

  def policy_inputs(self, trajectory, values):
    """Create inputs to policy model from a TrajectoryNp and values.

    Args:
      trajectory: a TrajectoryNp, the trajectory to create inputs from
      values: a numpy array: value function computed on trajectory

    Returns:
      a tuple of numpy arrays of the form (inputs, x1, x2, ...) that will be
      passed to the policy model; policy model will compute outputs from
      inputs and (outputs, x1, x2, ...) will be passed to self.policy_loss
      which should be overridden accordingly.
    """
    return NotImplementedError

  def policy_batches_stream(self):
    """Use the RLTask self._task to create inputs to the policy model."""
    # Maximum slice length for policy is max_slice_len + the added policy len.
    max_slice_length = self._max_slice_length + self._added_policy_slice_length
    for np_trajectory in self._task.trajectory_batch_stream(
        self._policy_batch_size,
        epochs=self._replay_epochs,
        max_slice_length=max_slice_length,
        include_final_state=False):
      (values, _) = self._run_value_model(
          np_trajectory.observations, np_trajectory.dist_inputs
      )
      values = self._aggregate_values(values)
      if len(values.shape) != 2:
        raise ValueError('Values are expected to have shape ' +
                         '[batch_size, length], got: %s' % str(values.shape))
      if values.shape[0] != self._policy_batch_size:
        raise ValueError('Values first dimension should = policy batch size, ' +
                         '%d != %d' %(values.shape[0], self._policy_batch_size))
      yield self.policy_inputs(np_trajectory, values)

  def train_epoch(self):
    """Trains RL for one epoch."""
    # Copy policy state accumulated during data collection to the trainer.
    self._policy_trainer.model_state = self._policy_collect_model.state

    # Copy policy weights and state to value trainer.
    if self._n_shared_layers > 0:
      _copy_model_weights_and_state(
          0, self._n_shared_layers, self._policy_trainer, self._value_trainer
      )

    # Update the target value network.
    self._value_eval_model.weights = self._value_trainer.model_weights
    self._value_eval_model.state = self._value_trainer.model_state

    n_value_evals = rl_training.remaining_evals(
        self._value_trainer.step,
        self._epoch,
        self._value_train_steps_per_epoch,
        self._value_evals_per_epoch)
    for _ in range(n_value_evals):
      self._value_trainer.train_epoch(
          self._value_train_steps_per_epoch // self._value_evals_per_epoch,
          self._value_eval_steps,
      )
    # Copy value weights and state to policy trainer.
    if self._n_shared_layers > 0:
      _copy_model_weights_and_state(
          0, self._n_shared_layers, self._value_trainer, self._policy_trainer
      )
    n_policy_evals = rl_training.remaining_evals(
        self._policy_trainer.step,
        self._epoch,
        self._policy_train_steps_per_epoch,
        self._policy_evals_per_epoch)
    # Check if there was a restart after value training finishes and policy not.
    stopped_after_value = (n_value_evals == 0 and
                           n_policy_evals < self._policy_evals_per_epoch)
    should_copy_weights = self._n_shared_layers > 0 and not stopped_after_value
    if should_copy_weights:
      _copy_model_weights_and_state(
          0, self._n_shared_layers, self._value_trainer, self._policy_trainer
      )

    # Update the target value network.
    self._value_eval_model.weights = self._value_trainer.model_weights
    self._value_eval_model.state = self._value_trainer.model_state

    for _ in range(n_policy_evals):
      self._policy_trainer.train_epoch(
          self._policy_train_steps_per_epoch // self._policy_evals_per_epoch,
          self._policy_eval_steps,
      )

  def close(self):
    self._value_trainer.close()
    super().close()


def _copy_model_weights_and_state(  # pylint: disable=invalid-name
    start, end, from_trainer, to_trainer, copy_optimizer_slots=False
):
  """Copy model weights[start:end] from from_trainer to to_trainer."""
  from_weights = from_trainer.model_weights
  to_weights = to_trainer.model_weights
  shared_weights = from_weights[start:end]
  to_weights[start:end] = shared_weights
  to_trainer.model_weights = to_weights

  from_state = from_trainer.model_state
  to_state = to_trainer.model_state
  shared_state = from_state[start:end]
  to_state[start:end] = shared_state
  to_trainer.model_state = to_state

  if copy_optimizer_slots:
    # TODO(lukaszkaiser): make a nicer API in Trainer to support this.
    # Currently we use the hack below. Note [0] since that's the model w/o loss.
    # pylint: disable=protected-access
    from_slots = from_trainer._opt_state.slots[0][start:end]
    to_slots = to_trainer._opt_state.slots[0]
    # The lines below do to_slots[start:end] = from_slots, but on tuples.
    new_slots = to_slots[:start] + from_slots[start:end] + to_slots[end:]
    new_slots = tuple([new_slots] + list(to_trainer._opt_state.slots[1:]))
    to_trainer._opt_state = to_trainer._opt_state._replace(slots=new_slots)
    # pylint: enable=protected-access


### Implementations of common actor-critic algorithms.


class AdvantageBasedActorCriticTrainer(ActorCriticTrainer):
  """Base class for advantage-based actor-critic algorithms."""

  def __init__(
      self,
      task,
      advantage_estimator=rl_advantages.td_lambda,
      advantage_normalization=True,
      advantage_normalization_epsilon=1e-5,
      **kwargs
  ):
    self._advantage_estimator = advantage_estimator
    self._advantage_normalization = advantage_normalization
    self._advantage_normalization_epsilon = advantage_normalization_epsilon
    super(AdvantageBasedActorCriticTrainer, self).__init__(task, **kwargs)

  def policy_inputs(self, trajectory, values):
    """Create inputs to policy model from a TrajectoryNp and values."""
    # How much TD to use is determined by the added policy slice length,
    # as the policy batches need to be this much longer to calculate TD.
    advantages = self._advantage_estimator(
        trajectory.rewards, trajectory.returns, values,
        gamma=self._task.gamma,
        n_extra_steps=self._added_policy_slice_length,
    )
    # Observations should be the same length as advantages - so if we are
    # using n_extra_steps, we need to trim the length to match.
    obs = trajectory.observations[:, :advantages.shape[1]]
    act = trajectory.actions[:, :advantages.shape[1]]
    mask = trajectory.mask[:, :advantages.shape[1]]  # Mask to zero-out padding.
    if trajectory.dist_inputs is not None:
      dist_inputs = trajectory.dist_inputs[:, :advantages.shape[1]]
    else:
      dist_inputs = jnp.zeros(advantages.shape + (self._policy_dist.n_inputs,))
    # Shape checks to help debugging.
    if len(advantages.shape) != 2:
      raise ValueError('Advantages are expected to have shape ' +
                       '[batch_size, length], got: %s' % str(advantages.shape))
    if act.shape[0:2] != advantages.shape:
      raise ValueError('First 2 dimensions of actions should be the same as in '
                       'advantages, %s != %s' % (act.shape[0:2],
                                                 advantages.shape))
    if obs.shape[0:2] != advantages.shape:
      raise ValueError('First 2 dimensions of observations should be the same '
                       'as in advantages, %s != %s' % (obs.shape[0:2],
                                                       advantages.shape))
    if dist_inputs.shape[:2] != advantages.shape:
      raise ValueError('First 2 dimensions of dist_inputs should be the same '
                       'as in advantages, %s != %s' % (dist_inputs.shape[:2],
                                                       advantages.shape))
    if mask.shape != advantages.shape:
      raise ValueError('Mask and advantages shapes should be the same'
                       ', %s != %s' % (mask.shape, advantages.shape))
    return (obs, act, advantages, dist_inputs, mask)

  @property
  def policy_loss_given_log_probs(self):
    """Policy loss given action log-probabilities."""
    raise NotImplementedError

  def _preprocess_advantages(self, advantages):
    if self._advantage_normalization:
      advantages = (
          (advantages - jnp.mean(advantages)) /
          (jnp.std(advantages) + self._advantage_normalization_epsilon)
      )
    return advantages

  @property
  def policy_loss(self, **unused_kwargs):
    """Policy loss."""
    def LossInput(dist_inputs, actions, advantages, old_dist_inputs):  # pylint: disable=invalid-name
      """Calculates action log probabilities and normalizes advantages."""
      advantages = self._preprocess_advantages(advantages)
      log_probs = self._policy_dist.log_prob(dist_inputs, actions)
      old_log_probs = self._policy_dist.log_prob(old_dist_inputs, actions)
      return (log_probs, advantages, old_log_probs)

    return tl.Serial(
        tl.Fn('LossInput', LossInput, n_out=3),
        # Policy loss is expected to consume
        # (log_probs, advantages, old_log_probs, mask).
        self.policy_loss_given_log_probs,
    )

  @property
  def policy_metrics(self):
    metrics = super(AdvantageBasedActorCriticTrainer, self).policy_metrics
    metrics.update({
        'advantage_mean': self.advantage_mean,
        'advantage_std': self.advantage_std,
    })
    return metrics

  @property
  def advantage_mean(self):
    return tl.Serial([
        # (dist_inputs, advantages, old_dist_inputs, mask)
        tl.Select([1]),  # Select just the advantages.
        tl.Fn('AdvantageMean', lambda x: jnp.mean(x)),  # pylint: disable=unnecessary-lambda
    ])

  @property
  def advantage_std(self):
    return tl.Serial([
        # (dist_inputs, advantages, old_dist_inputs, mask)
        tl.Select([1]),  # Select just the advantages.
        tl.Fn('AdvantageStd', lambda x: jnp.std(x)),  # pylint: disable=unnecessary-lambda
    ])


class A2CTrainer(AdvantageBasedActorCriticTrainer):
  """Trains policy and value models using the A2C algortithm."""

  on_policy = True

  def __init__(self, task, entropy_coeff=0.01, **kwargs):
    """Configures the A2C Trainer."""
    self._entropy_coeff = entropy_coeff
    super(A2CTrainer, self).__init__(task, **kwargs)

  @property
  def policy_loss_given_log_probs(self):
    """Definition of the Advantage Actor Critic (A2C) loss."""
    # A2C is one of the most basic actor-critic RL algorithms.
    # TODO(henrykm) re-factor f into rl_layers and finally share code between
    # actor_critic.py and actor_critic_joint.py - requires change of inputs
    # in actor_critic_joint.py from dist_inputs to log_probs.
    def f(log_probs, advantages, old_log_probs, mask):
      del old_log_probs  # Not used in A2C.
      # log_probs of the shape float32[128,1]
      # advantages of the shape int32[128,1]
      # mask of the shape int32[128,1]
      if log_probs.shape != advantages.shape:
        raise ValueError('New log-probs and advantages shapes '
                         'should be the same, %s != %s' % (log_probs.shape,
                                                           advantages.shape))
      if log_probs.shape != mask.shape:
        raise ValueError('New log-probs and mask shapes should be the same'
                         ', %s != %s' % (log_probs.shape, mask.shape))

      a2c_objective = -jnp.sum(log_probs * advantages * mask) / jnp.sum(mask)

      entropy_vec = self._policy_dist.entropy(log_probs) * self._entropy_coeff
      entropy_loss = jnp.mean(entropy_vec)

      combined_loss = a2c_objective - entropy_loss

      return combined_loss

    return tl.Fn('A2CLoss', f)


class PPOTrainer(AdvantageBasedActorCriticTrainer):
  """The Proximal Policy Optimization Algorithm aka PPO.

  Trains policy and value models using the PPO algortithm.
  """

  on_policy = True

  def __init__(self, task, epsilon=0.2, entropy_coeff=0.01, **kwargs):
    """Configures the PPO Trainer."""
    self._entropy_coeff = entropy_coeff
    self._epsilon = epsilon
    super(PPOTrainer, self).__init__(task, **kwargs)

  @property
  def policy_loss_given_log_probs(self):
    """Definition of the Proximal Policy Optimization loss."""
    def f(new_log_probs, advantages, old_log_probs, mask):
      # new_log_probs of the shape float32[128,1]
      # advantages of the shape int32[128,1]
      # old_log_probs of the shape int32[128,1]
      # mask of the shape int32[128,1]
      if new_log_probs.shape != advantages.shape:
        raise ValueError('New log-probs and advantages shapes '
                         'should be the same, %s != %s' % (new_log_probs.shape,
                                                           advantages.shape))
      if new_log_probs.shape != old_log_probs.shape:
        raise ValueError('New log-probs and old log-probs shapes '
                         'should be the same, %s != %s' % (new_log_probs.shape,
                                                           old_log_probs.shape))
      if new_log_probs.shape != mask.shape:
        raise ValueError('New log-probs and mask shapes should be the same'
                         ', %s != %s' % (new_log_probs.shape, mask.shape))

      # The ratio between new_probs and old_probs expressed
      # using log_probs and exponentaion
      probs_ratio = jnp.exp(new_log_probs - old_log_probs)
      if advantages.shape != probs_ratio.shape:
        raise ValueError('New log-probs and old log probs shapes '
                         'should be the same, %s != %s' % (advantages.shape,
                                                           probs_ratio.shape))
      unclipped_objective = probs_ratio * advantages
      clipped_objective = jnp.clip(probs_ratio,
                                   1 - self._epsilon,
                                   1 + self._epsilon) * advantages

      if unclipped_objective.shape != probs_ratio.shape:
        raise ValueError('unclipped_objective and clipped_objective shapes '
                         'should be the same, %s != %s' % (
                             unclipped_objective.shape,
                             clipped_objective.shape))

      ppo_objective = jnp.minimum(unclipped_objective, clipped_objective)

      if ppo_objective.shape != mask.shape:
        raise ValueError('ppo_objective and mask shapes '
                         'should be the same, %s != %s' % (
                             ppo_objective.shape,
                             mask.shape))

      ppo_loss = -jnp.sum(ppo_objective * mask) / jnp.sum(mask)
      entropy_vec = self._policy_dist.entropy(
          new_log_probs) * self._entropy_coeff
      entropy_loss = jnp.mean(entropy_vec)
      combined_loss = ppo_loss - entropy_loss

      return combined_loss
    return tl.Fn('PPOLoss', f)


# AWR is an off-policy actor-critic RL algorithm.
def awr_weights(advantages, beta):
  return jnp.exp(advantages / beta)


# Helper functions for computing AWR metrics.
def awr_metrics(beta):
  return {  # pylint: disable=g-complex-comprehension
      'awr_weight_' + name: awr_weight_stat(name, fn, beta)
      for (name, fn) in [
          ('mean', jnp.mean),
          ('std', jnp.std),
          ('min', jnp.min),
          ('max', jnp.max),
      ]
  }


def awr_weight_stat(stat_name, stat_fn, beta):
  return tl.Serial([
      tl.Select([1]),  # Select just the advantages.
      tl.Fn(
          'AWRWeight' + stat_name.capitalize(),
          lambda x: stat_fn(awr_weights(x, beta)),
      ),
  ])


def AWRLoss(beta, w_max):  # pylint: disable=invalid-name
  """Definition of the Advantage Weighted Regression (AWR) loss."""
  def f(log_probs, advantages, old_log_probs, mask):
    del old_log_probs  # Not used in AWR.
    weights = jnp.minimum(awr_weights(advantages, beta), w_max)
    return -jnp.sum(log_probs * weights * mask) / jnp.sum(mask)
  return tl.Fn('AWRLoss', f)


class AWRTrainer(AdvantageBasedActorCriticTrainer):
  """Trains policy and value models using AWR."""

  on_policy = False

  def __init__(self, task, beta=1.0, w_max=20.0, **kwargs):
    """Configures the AWR Trainer."""
    self._beta = beta
    self._w_max = w_max
    super(AWRTrainer, self).__init__(task, **kwargs)

  @property
  def policy_loss_given_log_probs(self):
    """Policy loss."""
    return AWRLoss(beta=self._beta, w_max=self._w_max)  # pylint: disable=no-value-for-parameter

  @property
  def policy_metrics(self):
    metrics = super(AWRTrainer, self).policy_metrics
    metrics.update(awr_metrics(self._beta))
    return metrics


class SamplingAWRTrainer(AdvantageBasedActorCriticTrainer):
  """Trains policy and value models using Sampling AWR."""

  on_policy = False

  def __init__(self, task, beta=1.0, w_max=20.0, **kwargs):
    """Configures the AWR Trainer."""
    self._beta = beta
    self._w_max = w_max
    super(SamplingAWRTrainer, self).__init__(task, q_value=True, **kwargs)

  @property
  def policy_metrics(self):
    metrics = super(SamplingAWRTrainer, self).policy_metrics
    metrics.update(awr_metrics(self._beta))
    return metrics

  @property
  def policy_loss(self, **unused_kwargs):
    """Policy loss."""
    def LossInput(dist_inputs, actions, advantages, old_dist_inputs, mask):  # pylint: disable=invalid-name
      """Calculates action log probabilities and normalizes advantages."""
      del old_dist_inputs
      advantages = self._preprocess_advantages(advantages)
      dist_inputs = jnp.broadcast_to(
          dist_inputs, (self._q_value_n_samples,) + dist_inputs.shape
      )
      log_probs = self._policy_dist.log_prob(dist_inputs, actions)
      # (batch_size, n_samples, ...) -> (n_samples, batch_size, ...)
      advantages = jnp.swapaxes(advantages, 0, 1)
      mask = jnp.swapaxes(mask, 0, 1)
      return (log_probs, advantages, log_probs, mask)

    return tl.Serial(
        tl.Fn('LossInput', LossInput, n_out=4),
        # Policy loss is expected to consume
        # (log_probs, advantages, old_log_probs, mask).
        AWRLoss(beta=self._beta, w_max=self._w_max),  # pylint: disable=no-value-for-parameter
    )

  def policy_batches_stream(self):
    """Use the RLTask self._task to create inputs to the policy model."""
    # For now TD-0 estimation of the value. TODO(pkozakowski): Support others?
    for np_trajectory in self._task.trajectory_batch_stream(
        self._policy_batch_size,
        epochs=self._replay_epochs,
        max_slice_length=self._max_slice_length,
        include_final_state=False,
    ):
      (q_values, actions) = self._run_value_model(
          np_trajectory.observations, np_trajectory.dist_inputs
      )
      # TODO(pkozakowski): Try max here.
      values = jnp.mean(q_values, axis=0)

      if len(values.shape) != 2:
        raise ValueError('Values are expected to have shape ' +
                         '[batch_size, length], got: %s' % str(values.shape))
      if values.shape[0] != self._policy_batch_size:
        raise ValueError('Values first dimension should = policy batch size, ' +
                         '%d != %d' %(values.shape[0], self._policy_batch_size))

      # q_values shape: (n_samples, batch_size, length)
      # values shape: (batch_size, length)
      # Computing advantages by broadcasting over n_samples.
      advantages = q_values - values
      mask = jnp.broadcast_to(np_trajectory.mask, advantages.shape)

      shapes.assert_shape_equals(
          advantages, (self._q_value_n_samples,) + values.shape
      )
      shapes.assert_same_shape(mask, advantages)

      # Swapping the n_samples and batch_size axes, so the input is split
      # between accelerators along the batch_size axis.
      advantages = jnp.swapaxes(advantages, 0, 1)
      mask = jnp.swapaxes(mask, 0, 1)

      yield (np_trajectory.observations, actions, advantages, mask, mask)
