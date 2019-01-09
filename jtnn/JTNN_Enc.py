from collections import deque

import torch
import torch.nn as nn

from nnutils import create_var, GRU

MAX_NUM_NEIGHBORS = 8

class JTNNEncoder(nn.Module):
    """
    Junction Tree Neural Network Encoder
    """

    def __init__(self, vocab, hidden_size, embedding=None):
        """
        The constructor for the class.

        Args:
            vocab: The cluster vocabulary over the entire training dataset.
            hidden_size: Dimension of the encoding space.
            embedding: Embedding space for encoding vocabulary composition.
        """
        # invoke the superclass constructor
        super(JTNNEncoder, self).__init__()

        # size of hidden "edge message vectors"
        self.hidden_size = hidden_size

        # size of the vocabulary of clusters
        self.vocab_size = vocab.size()

        # the entire vocabulary of clusters
        self.vocab = vocab

        if embedding is None:
            self.embedding = nn.Embedding(self.vocab_size, hidden_size)
        else:
            self.embedding = embedding

        # all the weight matrices for the GRU
        self.W_z = nn.Linear(2 * hidden_size, hidden_size)
        self.W_r = nn.Linear(hidden_size, hidden_size, bias=False)
        self.U_r = nn.Linear(hidden_size, hidden_size)
        self.W_h = nn.Linear(2 * hidden_size, hidden_size)
        self.W = nn.Linear(2 * hidden_size, hidden_size)

    def forward(self, root_batch):
        """
        Args:
            root_batch: list / batch of root nodes of the corresponding junction trees of the batch of molecules.

        Returns:
            h: Dictionary containing hidden message vectors for all the edges, of all the junction trees, across the
               entire training dataset.
            root_vecs; Root vectors for all the junction trees, across the entire training dataset.
        """
        # list to store bottom-up & top-down traversal order for each junction tree

        traversal_order_list = []

        # get the traversal order for each junction tree given root node
        for root in root_batch:
            traversal_order = self.get_bottom_up_top_down_traversal_order(root)
            traversal_order_list.append(traversal_order)

        # dictionary for storing hidden messages along various edges
        h = {}

        max_iter = max([len(traversal_order) for traversal_order in traversal_order_list])

        # if no messages from any neighbor node, then use this vector of zeros as
        # neighbor message vector
        padding = create_var(torch.zeros(self.hidden_size), False)

        for iter in range(max_iter):

            edge_tuple_list = []

            for traversal_order in traversal_order_list:
                # keep appending traversal orders for a particular depth level,
                # from a given traversal_order list,
                # until the list is not empty
                if iter < len(traversal_order):
                    edge_tuple_list.extend(traversal_order[iter])

            # for each edge, list of wids (word_idx corresponding to the cluster vocabulary item) of the current node.
            cur_x = []

            # hidden messages for the current iteration, for the junction trees, across the entire dataset.
            cur_h_nei = []

            for node_x, node_y in edge_tuple_list:
                x, y = node_x.idx, node_y.idx
                # wid is the index of the SMILES string, corresponding to the vocabulary cluster
                # of the node
                cur_x.append(node_x.wid)

                # hidden messages from predecessor neighbor nodes of x, to x
                h_nei = []

                for node_z in node_x.neighbors:
                    z = node_z.idx
                    if z == y:
                        continue
                    # hidden message from predecessor neighbor node z to node x
                    h_nei.append(h[(z, x)])

                # each node can have at most MAX_NUM_NEIGHBORS(= 8) neighbors
                # thus we have a fixed construct of 8 message vectors
                # if a node doesn't receive messages from all of 8 neighbors,
                # then we set these message vectors to the zero vector
                pad_len = MAX_NUM_NEIGHBORS - len(h_nei)
                h_nei.extend([padding] * pad_len)

                # append the chunk of hidden message vectors from neighbors to the cur_h_nei list
                # for batch operation
                cur_h_nei.extend(h_nei)

            # for each wid in the list, get the corresponding word embedding
            cur_x = create_var((torch.tensor(cur_x)))
            cur_x = self.embedding(cur_x)

            # hidden edge message vector for this iteration, for all the junction trees, across the entire
            # training dataset.
            cur_h_nei = torch.cat(cur_h_nei, dim=0).view(-1, MAX_NUM_NEIGHBORS, self.hidden_size)

            # calculate the hidden messages for the next iteration, using the GRU operation.
            new_h = GRU(cur_x, cur_h_nei, self.W_z, self.W_r, self.U_r, self.W_h)

            # put the hidden messages for the next iteration, in the dictionary.
            for idx, edge in enumerate(edge_tuple_list):
                x, y = edge[0].idx, edge[1].idx
                h[(x, y)] = new_h[idx]

        # evaluate root vectors encoding the structure for all the junction tress, across the entire training dataset.
        root_vecs = self.evaluate_root_vecs(root_batch, h, self.embedding, self.W)

        return h, root_vecs

    def get_bottom_up_top_down_traversal_order(self, root):
        """
        This method, gets the bottom-up and top-down traversal order for tree message passing purposes.

        * node.idx is the id of the node across all nodes, of all junction trees, for all molecules of the dataset.

        Args:
        root: Root of junction tree of a molecule in the training dataset.

        Returns:
            traversal_order: List of lists of tuples. Each sublist of tuples corresponds to a depth of junction tree.
                            Each tuple corresponds to an edge along which message passing occurs.
        """

        # FIFO queue for BFS traversal
        fifo_queue = deque([root])

        # set to keep track of visited nodes
        visited = set([root.idx])

        # root node is at zeroth depth
        root.depth = 0

        # list to store appropriate traversal order
        top_down, bottom_up = [], []

        while len(fifo_queue) > 0:
            # pop node from front of the queue
            x = fifo_queue.popleft()

            # traverse the neighbors
            for y in x.neighbors:
                if y.idx not in visited:
                    fifo_queue.append(y)

                    visited.add(y.idx)

                    y.depth = x.depth + 1

                    if y.depth > len(top_down):
                        # have a separate sublist for every depth
                        top_down.append([])
                        bottom_up.append([])

                    top_down[y.depth - 1].append((x, y))
                    bottom_up[y.depth - 1].append((y, x))

        # first we implement bottom-up traversal and then top-down traversal
        traversal_order = bottom_up[::-1] + top_down

        return traversal_order

    def evaluate_root_vecs(self, root_batch, h, embedding, W):
        """
        This method, returns the hidden vectors for the root nodes for all the junction trees, across the entire
        training dataset.

        Args:
            root_batch: list / batch of root nodes of the corresponding junction trees of the batch of molecules
            h: dictionary of hidden messages along all the edges, of all the junction trees, across the entire training dataset.
            embedding: embedding space for vocabulary composition
            W: weight matrix for calculating the hidden vectors for the root nodes of the junction trees, across the entire
            training dataset.

        Returns:
            root_vecs: Hidden vectors for the root nodes of all the junction trees, across all the molecules of the
            training dataset.
        """

        # for each root node, store the idx of the corresponding cluster vocabulary item.
        x_idx = []

        # list to store lists of hidden edge message vectors from neighbors to root, for all root
        # nodes in the root_batch
        h_nei = []

        hidden_size = embedding.embedding_dim

        padding = create_var(torch.zeros(hidden_size), False)

        for root in root_batch:
            x_idx.append(root.wid)

            # list to store hidden edge messages from neighbors of each root node
            hidden_edge_messages = [h[(node_y.idx, root.idx)] for node_y in root.neighbors]

            # each node can have at most MAX_NUM_NEIGHBORS(= 8 ) neighbors
            # thus we have a fixed construct of 8 message vectors
            # if a node doesn't receive messages from all of 8 neighbors,
            # then we set these message vectors to the zero vector
            pad_len = MAX_NUM_NEIGHBORS - len(hidden_edge_messages)
            hidden_edge_messages.extend([padding] * pad_len)
            h_nei.extend(hidden_edge_messages)

        h_nei = torch.cat(h_nei, dim=0).view(-1, MAX_NUM_NEIGHBORS, hidden_size)

        sum_h_nei = h_nei.sum(dim=1)

        x_vec = create_var(torch.LongTensor(x_idx))

        x_vec = embedding(x_vec)

        root_vecs = torch.cat([x_vec, sum_h_nei], dim=1)
        return nn.ReLU()(W(root_vecs))
