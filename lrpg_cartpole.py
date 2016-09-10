#!/usr/bin/env python
import argparse
import bullet_cartpole
import datetime
import gym
import json
import numpy as np
import sys
import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow.python.ops import init_ops
import time
import util
import collections

np.set_printoptions(precision=3, threshold=10000, suppress=True, linewidth=10000)

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--num-hidden', type=int, default=32)
parser.add_argument('--num-eval', type=int, default=0,
                    help="if >0 just run this many episodes with no training")
parser.add_argument('--num-train-batches', type=int, default=10,
                    help="number of training batches to run")
parser.add_argument('--rollouts-per-batch', type=int, default=10,
                    help="number of rollouts to run for each training batch")
parser.add_argument('--ckpt-dir', type=str, default=None,
                    help="if set save ckpts to this dir")
parser.add_argument('--ckpt-freq', type=int, default=300,
                    help="freq (sec) to save ckpts")
bullet_cartpole.add_opts(parser)
opts = parser.parse_args()
sys.stderr.write("%s\n" % opts)
assert not opts.use_raw_pixels, "TODO: add convnet from ddpg here"

class LikelihoodRatioPolicyGradientAgent(object):
  def __init__(self, env, hidden_dim, optimiser, gui=False):
    self.env = env
    self.gui = gui

    num_actions = self.env.action_space.n

    # we have three place holders we'll use...
    # observations; used either during rollout to sample some actions, or
    # during training when combined with actions_taken and advantages.
    shape_with_batch = [None] + list(self.env.observation_space.shape)
    self.observations = tf.placeholder(shape=shape_with_batch,
                                       dtype=tf.float32)
    # the actions we took during rollout
    self.actions = tf.placeholder(tf.int32, name='actions')
    # the advantages we got from taken 'action_taken' in 'observation'
    self.advantages = tf.placeholder(tf.float32, name='advantages')

    # our model is a very simple MLP
    with tf.variable_scope("model"):
      flat_obs = slim.flatten(self.observations)
      hidden1 = slim.fully_connected(inputs=flat_obs,
                                     num_outputs=hidden_dim,
                                     biases_initializer=init_ops.constant_initializer(0.1))
      hidden2 = slim.fully_connected(inputs=hidden1,
                                     num_outputs=hidden_dim,
                                     biases_initializer=init_ops.constant_initializer(0.1))
      logits = slim.fully_connected(inputs=hidden2,
                                    num_outputs=num_actions,
                                    activation_fn=None)

    # in the eval case just pick arg max
    self.action_argmax = tf.argmax(logits, 1)

    # for rollouts we need an op that samples actions from this
    # model to give a stochastic action.
    sample_action = tf.multinomial(logits, num_samples=1)
    self.sampled_action_op = tf.reshape(sample_action, shape=[])

    # we are trying to maximise the product of two components...
    # 1) the log_p of "good" actions.
    # 2) the advantage term based on the rewards from actions.

    # first we need the log_p values for each observation for the actions we specifically
    # took by sampling... we first run a log_softmax over the action logits to get
    # probabilities.
    log_softmax = tf.nn.log_softmax(logits)
    self.debug_softmax = tf.exp(log_softmax)

    # we then use a mask to only select the elements of the softmaxs that correspond
    # to the actions we actually took. we could also do this by complex indexing and a
    # gather but i always think this is more natural. the "cost" of dealing with the
    # mostly zero one hot, as opposed to doing a gather on sparse indexes, isn't a big
    # deal when the number of observations is >> number of actions.
    action_mask = tf.one_hot(indices=self.actions, depth=num_actions)
    action_log_prob = tf.reduce_sum(log_softmax * action_mask, reduction_indices=1)

    # the (element wise) product of these action log_p's with the total reward of the
    # episode represents the quantity we want to maximise. we standardise the advantage
    # values so roughly 1/2 +ve / -ve as a variance control.
    action_mul_advantages = tf.mul(action_log_prob,
                                   util.standardise(self.advantages))
    self.loss = -tf.reduce_sum(action_mul_advantages)  # recall: we are maximising.
    with tf.variable_scope("optimiser"):
      self.train_op = optimiser.minimize(self.loss)

  def sample_action_given(self, observation, sampling):
    """ sample one action given observation"""
    if not sampling:
      # pure argmax eval
      am, sm = tf.get_default_session().run([self.action_argmax, self.debug_softmax],
                                             feed_dict={self.observations: [observation]})
      print "EVAL sm ", sm, "argmax", am[0]
      return am[0]

    # epilson greedy "noise" will do for this simple case..
    if (np.random.random() < 0.1):
      return self.env.action_space.sample()

    # sample from logits
    return tf.get_default_session().run(self.sampled_action_op,
                                        feed_dict={self.observations: [observation]})

  def rollout(self, sampling=True):
    """ run one episode collecting observations, actions and advantages"""
    observations, actions, rewards = [], [], []
    observation = self.env.reset()
    done = False
    while not done:
      observations.append(observation)
      action = self.sample_action_given(observation, sampling)
      if action == 5:
        print >>sys.stderr, "FAIL! (multinomial logits sampling bug?)"
        action = 0
      observation, reward, done, _ = self.env.step(action)
      actions.append(action)
      rewards.append(reward)
      if self.gui:
        self.env.render()
    return observations, actions, rewards

  def train(self, observations, actions, advantages):
    """ take one training step given observations, actions and subsequent advantages"""
    _, loss = tf.get_default_session().run([self.train_op, self.loss],
                                           feed_dict={self.observations: observations,
                                                      self.actions: actions,
                                                      self.advantages: advantages})
    return float(loss)

  def run_training(self, num_batches, rollouts_per_batch, saver_util):
    for batch_id in xrange(num_batches):
      self.run_eval(1)

      # perform a number of rollouts
      batch_observations, batch_actions, batch_advantages = [], [], []
      total_rewards = []
      for _ in xrange(rollouts_per_batch):
        observations, actions, rewards = self.rollout()
        batch_observations += observations
        batch_actions += actions
        # train with advantages, not per observation/action rewards.
        # _every_ observation/action in this rollout gets assigned
        # the _total_ reward of the episode. (crazy that this works!)
        batch_advantages += [sum(rewards)] * len(rewards)
        # keep total rewards just for debugging / stats
        total_rewards.append(sum(rewards))

      if min(total_rewards) == max(total_rewards):
        # converged ??
        sys.stderr.write("converged? standardisation of advantaged will barf here....\n")
        loss = 0
      else:
        loss = self.train(batch_observations, batch_actions, batch_advantages)

      # dump some stats
      stats = collections.OrderedDict()
      stats["time"] = int(time.time())
      stats["batch"] = batch_id
      stats["mean_batch"] = np.mean(total_rewards)
      stats["rewards"] = total_rewards
      stats["loss"] = loss
      print "STATS %s\t%s" % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              json.dumps(stats))

      # save if required
      if saver_util is not None:
        saver_util.save_if_required()

  def run_eval(self, num_eval):
    for _ in xrange(num_eval):
      _, _, rewards = self.rollout(sampling=False)
      print sum(rewards)


def main():
  env = bullet_cartpole.BulletCartpole(opts=opts, discrete_actions=True)

  with tf.Session() as sess:
    agent = LikelihoodRatioPolicyGradientAgent(env=env, gui=opts.gui,
                                               hidden_dim=opts.num_hidden,
                                               optimiser=tf.train.AdamOptimizer())

    # setup saver util; will load latest ckpt, or init if none...
    saver_util = None
    if opts.ckpt_dir is not None:
      saver_util = util.SaverUtil(sess, opts.ckpt_dir, opts.ckpt_freq)
    else:
      sess.run(tf.initialize_all_variables())

    # run either eval or training
    if opts.num_eval > 0:
      agent.run_eval(opts.num_eval)
    else:
      agent.run_training(opts.num_train_batches, opts.rollouts_per_batch,
                         saver_util)
      if saver_util is not None:
        saver_util.force_save()

if __name__ == "__main__":
  main()