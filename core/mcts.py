import torch

import numpy as np
import core.ctree.cytree as tree

from torch.cuda.amp import autocast as autocast

import time

class MCTS(object):
    def __init__(self, config):
        self.config = config

    def search(self, roots, model, hidden_state_roots, reward_hidden_roots):
        """Do MCTS for the roots (a batch of root nodes in parallel). Parallel in model inference
        Parameters
        ----------
        roots: Any
            a batch of expanded root nodes
        hidden_state_roots: list
            the hidden states of the roots
        reward_hidden_roots: list
            the value prefix hidden states in LSTM of the roots
        """

        # Number of searches per simulation
        searches = 2
        # before_search = time.perf_counter()

        with torch.no_grad():
            model.eval()

            # preparation
            num = roots.num
            device = self.config.device
            pb_c_base, pb_c_init, discount = self.config.pb_c_base, self.config.pb_c_init, self.config.discount
            # the data storage of hidden states: storing the states of all the tree nodes
            prev_hidden_state_shape = list(hidden_state_roots.shape)
            hidden_state_shape = [searches] + prev_hidden_state_shape
            hidden_state_pool = [np.vstack([hidden_state_roots] * searches).reshape(hidden_state_shape)]
            # 1 x batch x 64
            # the data storage of value prefix hidden states in LSTM
            reward_hidden_c_pool = [np.vstack([reward_hidden_roots[0]] * searches).reshape(1, searches, num, -1)]
            reward_hidden_h_pool = [np.vstack([reward_hidden_roots[1]] * searches).reshape(1, searches, num, -1)]
            # the index of each layer in the tree
            hidden_state_index_x = 0
            # minimax value storage
            min_max_stats_lst = tree.MinMaxStatsList(num, searches)
            min_max_stats_lst.set_delta(self.config.value_delta_max)
            horizons = self.config.lstm_horizon_len

            for index_simulation in range(self.config.num_simulations // searches):
                hidden_states = []
                hidden_states_c_reward = []
                hidden_states_h_reward = []

                results = tree.ResultsWrapper(num, searches)
                
                hidden_state_index_x_lst, hidden_state_index_y_lst, last_actions = tree.batch_traverse(roots, pb_c_base, pb_c_init, discount, min_max_stats_lst, results)
                
                # obtain the search horizon for leaf nodes
                search_lens = results.get_search_len()

                # obtain the states for leaf nodes
                for i in range(searches):
                    for ix, iy in zip(hidden_state_index_x_lst[i], hidden_state_index_y_lst[i]):
                        hidden_states.append(hidden_state_pool[ix][i][iy])
                        hidden_states_c_reward.append(reward_hidden_c_pool[ix][0][i][iy])
                        hidden_states_h_reward.append(reward_hidden_h_pool[ix][0][i][iy])

                hidden_states = torch.from_numpy(np.asarray(hidden_states)).to(device).float()
                hidden_states_c_reward = torch.from_numpy(np.asarray(hidden_states_c_reward)).to(device).unsqueeze(0)
                hidden_states_h_reward = torch.from_numpy(np.asarray(hidden_states_h_reward)).to(device).unsqueeze(0)
                last_actions = torch.from_numpy(np.asarray(last_actions)).to(device).view(-1).unsqueeze(1).long()

                # evaluation for leaf nodes
                if self.config.amp_type == 'torch_amp':
                    with autocast():
                        network_output = model.recurrent_inference(hidden_states, (hidden_states_c_reward, hidden_states_h_reward), last_actions)
                else:
                    network_output = model.recurrent_inference(hidden_states, (hidden_states_c_reward, hidden_states_h_reward), last_actions)

                hidden_state_nodes = network_output.hidden_state.reshape(hidden_state_shape)
                value_prefix_pool = network_output.value_prefix.reshape(searches, -1).tolist()
                value_pool = network_output.value.reshape(searches, -1).tolist()
                policy_logits_pool = network_output.policy_logits.reshape(searches, num, -1).tolist()
                reward_hidden_nodes = network_output.reward_hidden

                hidden_state_pool.append(hidden_state_nodes)
                # reset 0
                # reset the hidden states in LSTM every horizon steps in search
                # only need to predict the value prefix in a range (eg: s0 -> s5)
                assert horizons > 0
                reset_idx = (np.array(search_lens) % horizons == 0)
                assert reset_idx.shape[0] == searches and reset_idx.shape[1] == num

                reward_hidden_nodes_0 = reward_hidden_nodes[0].reshape(1, searches, num, -1)
                reward_hidden_nodes_1 = reward_hidden_nodes[1].reshape(1, searches, num, -1)

                reward_hidden_nodes_0[:, reset_idx, :] = 0
                reward_hidden_nodes_1[:, reset_idx, :] = 0
                is_reset_lst = reset_idx.astype(np.int32).tolist()

                reward_hidden_c_pool.append(reward_hidden_nodes_0)
                reward_hidden_h_pool.append(reward_hidden_nodes_1)
                hidden_state_index_x += 1

                # backpropagation along the search path to update the attributes
                tree.batch_back_propagate(hidden_state_index_x, discount,
                                          value_prefix_pool, value_pool, policy_logits_pool,
                                          min_max_stats_lst, results, is_reset_lst)
                
                
        # after_search = time.perf_counter()
        # print(f'search time: {after_search - before_search} seconds')
