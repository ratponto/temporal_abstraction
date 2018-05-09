import numpy as np
import tensorflow as tf
from tools.agent_utils import get_mode, update_target_graph_aux, update_target_graph_sf, \
  update_target_graph_option, discount, reward_discount, set_image, make_gif
import os

from agents.base_agent import BaseAgent
import matplotlib.patches as patches
import matplotlib.pylab as plt
import numpy as np
from collections import deque
import seaborn as sns
sns.set()
import random
# import matplotlib.pyplot as plt
import copy
from tools.agent_utils import update_target_graph_reward
FLAGS = tf.app.flags.FLAGS


class BehaviourDynAgent(BaseAgent):
  def __init__(self, game, thread_id, global_step, config, global_network, barrier):
    super(BehaviourDynAgent, self).__init__(game, thread_id, global_step, config, global_network)
    self.barrier = barrier

  def init_play(self, sess, saver):
    self.sess = sess
    self.saver = saver
    self.episode_count = sess.run(self.global_step)

    if self.config.move_goal_nb_of_ep and self.config.multi_task:
      self.goal_position = self.env.set_goal(self.episode_count, self.config.move_goal_nb_of_ep)

    self.total_steps = sess.run(self.total_steps_tensor)
    tf.logging.info("Starting worker " + str(self.thread_id))
    self.behaviour_episode_buffer = deque()
    self.ms_aux = self.ms_sf = None

  def init_episode(self):
    self.done = False
    self.episode_len = 0

  def add_SF(self, sf):
    if self.config.eigen_approach == "SVD":
      self.global_network.sf_matrix_buffer[0] = sf.copy()
      self.global_network.sf_matrix_buffer = np.roll(self.global_network.sf_matrix_buffer, 1, 0)
    else:
      ci = np.argmax(
        [self.cosine_similarity(sf, d) for d in self.global_network.directions])

      sf_norm = np.linalg.norm(sf)
      sf_normalized = sf / (sf_norm + 1e-8)
      self.global_network.directions[ci] = self.config.tau * sf_normalized + (1 - self.config.tau) * \
                                                                             self.global_network.directions[ci]
      self.directions = self.global_network.directions

  def sync_threads(self, force=False):
    if force:
      self.sess.run(self.update_local_vars_aux)
      self.sess.run(self.update_local_vars_sf)
    else:
      if self.total_steps % self.config.target_update_iter_aux_behaviour == 0:
        self.sess.run(self.update_local_vars_aux)
      if self.total_steps % self.config.target_update_iter_sf_behaviour == 0:
        self.sess.run(self.update_local_vars_sf)

  def play(self, sess, coord, saver):
    with sess.as_default(), sess.graph.as_default():
      self.init_play(sess, saver)

      while not coord.should_stop():
        self.sync_threads()

        if self.episode_count > 0:
          self.recompute_eigenvectors_dynamic_SVD()

        self.init_episode()

        s, s_idx = self.env.reset()
        self.option_evaluation(s, s_idx)
        while not self.done:
          self.sync_threads()

          self.policy_evaluation(s)

          s1, r, self.done, s1_idx = self.env.step(self.action)

          if self.done:
            s1 = s

          if self.total_steps > self.config.observation_steps:
            self.old_option = self.option

            self.o_term = np.random.uniform() > 0.5

            if not self.done and self.o_term:
              self.option_evaluation(s1, s1_idx)

            self.store_general_info(s, self.old_option, s1, self.option, self.action, r, self.done)
            if len(self.behaviour_episode_buffer) > self.config.observation_steps and \
                        self.total_steps % self.config.behaviour_update_freq == 0:
              self.ms_aux, self.aux_loss, self.ms_sf, self.sf_loss = self.train()

            if self.total_steps % self.config.steps_summary_interval == 0:
              self.write_step_summary(self.ms_sf, self.ms_aux)

          s = s1
          self.episode_len += 1
          self.total_steps += 1
          sess.run(self.increment_total_steps_tensor)

        if self.episode_count % self.config.move_goal_nb_of_ep == 0 and self.episode_count != 0:
          tf.logging.info("Moving GOAL....")
          self.barrier.wait()
          self.goal_position = self.env.set_goal(self.episode_count, self.config.move_goal_nb_of_ep)

        if self.episode_count % self.config.episode_summary_interval == 0 and self.total_steps != 0 and self.episode_count != 0:
          self.write_episode_summary(self.ms_sf, self.ms_aux)

        self.episode_count += 1

  def option_evaluation(self, s, s_idx):
    self.option = np.random.choice(range(self.nb_options))

  def policy_evaluation(self, s):
    self.action = np.random.choice(range(self.action_size))
    sf = self.sess.run(self.local_network.sf, feed_dict={self.local_network.observation: np.stack([s])})[0]
    self.add_SF(sf)

  def store_general_info(self, s, o, s1, o1, a, r, d):
    if len(self.behaviour_episode_buffer) == self.config.memory_size:
      self.behaviour_episode_buffer.popleft()

    self.behaviour_episode_buffer.append([s, o, s1, o1, a, r, d])

  def write_step_summary(self, ms_sf, ms_aux):
    self.summary = tf.Summary()
    if ms_sf is not None:
      self.summary_writer.add_summary(ms_sf, self.total_steps)
    if ms_aux is not None:
      self.summary_writer.add_summary(ms_aux, self.total_steps)

    self.summary_writer.add_summary(self.summary, self.total_steps)
    self.summary_writer.flush()

  def write_episode_summary(self, ms_sf, ms_aux):
    self.summary = tf.Summary()
    self.summary.value.add(tag='Perf/Goal_position', simple_value=self.goal_position)

    self.summary_writer.add_summary(self.summary, self.episode_count)
    self.summary_writer.flush()
    self.write_step_summary(ms_sf, ms_aux)

  def recompute_eigenvectors_dynamic_SVD(self):
    if self.config.eigen:
      feed_dict = {self.local_network.matrix_sf: [self.global_network.sf_matrix_buffer]}
      eigenvect = self.sess.run(self.local_network.eigenvectors,
                                          feed_dict=feed_dict)
      eigenvect = eigenvect[0]

      # eigenvalues = eigenval[self.config.first_eigenoption:self.config.nb_options + self.config.first_eigenoption]
      new_eigenvectors = eigenvect[self.config.first_eigenoption:self.config.nb_options + self.config.first_eigenoption]
      min_similarity = np.min(
        [self.cosine_similarity(a, b) for a, b in zip(self.global_network.directions, new_eigenvectors)])
      max_similarity = np.max(
        [self.cosine_similarity(a, b) for a, b in zip(self.global_network.directions, new_eigenvectors)])
      mean_similarity = np.mean(
        [self.cosine_similarity(a, b) for a, b in zip(self.global_network.directions, new_eigenvectors)])
      self.summary = tf.Summary()
      self.summary.value.add(tag='Eigenvectors/Min similarity', simple_value=float(min_similarity))
      self.summary.value.add(tag='Eigenvectors/Max similarity', simple_value=float(max_similarity))
      self.summary.value.add(tag='Eigenvectors/Mean similarity', simple_value=float(mean_similarity))
      self.summary_writer.add_summary(self.summary, self.episode_count)
      self.summary_writer.flush()
      self.global_network.directions = new_eigenvectors
      self.directions = self.global_network.directions

