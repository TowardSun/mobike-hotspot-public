# -*- coding: utf-8 -*-

import os
import tensorflow as tf
import numpy as np
from models.utils import batch_generator
from models.data_util import load_city_pair_data
from models.utils import generate_combine_data, dann_evaluate, save_grid_search_res
from util.plot_util import plot_losses
from models.dann_model import DaCityModel, Solver, evaluate
import logging
from sklearn.model_selection import ParameterGrid, ParameterSampler
from sklearn.model_selection import train_test_split
import datetime

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s',
                    datefmt='%a, %d %b %Y %H:%M:%S')

logger = logging.getLogger(__name__)

__metaclass__ = type


"""
Description:
    City domain adaptation model:
        1. Feature network component: multi-layer feed forward neural network;
        2. Density network component: regression loss and ranking loss;
        3. Domain network component: city domain adaption by the gradient reversal layer;
Author: Zhaoyang Liu
Created time: 2017-12-10
"""


class DaCityRankingModel(DaCityModel):
    """
    Mobike hotspot detection city domain adaptation model.
    """

    def __init__(self, input_dim, output_dim=1, init_learning_rate=0.001, optimizer='adam',
                 batch_size=128, task_type='reg', pos_weight=3, use_batch_norm=True,
                 feature_layers=(32, 32), feature_dim=64, feature_dropout=0.2,
                 predictor_layers=(32, 16), predictor_dropout=0.2,
                 domain_layers=(16,), init_alpha=0.1, beta=0.5, tensor_board=False, rank_loss_type='sigmoid',
                 alpha_mode='static'):
        super(DaCityRankingModel, self).__init__(
            input_dim, output_dim, init_learning_rate, optimizer, batch_size, task_type, pos_weight, use_batch_norm,
            feature_layers, feature_dim, feature_dropout, predictor_layers, predictor_dropout, domain_layers, beta,
            tensor_board
        )

        self.regular_loss = None
        self.y_rank = None
        self.alpha = None

        self.init_alpha = init_alpha
        self.alpha_mode = alpha_mode
        self.rank_loss_type = rank_loss_type
        # self.build_model()

    def init_variable(self):
        super(DaCityRankingModel, self).init_variable()
        self.alpha = tf.placeholder(tf.float32, [])
        self.learning_rate = tf.placeholder(tf.float32, [])
        self.y_rank = tf.placeholder(tf.float32, [None, 1])

    def _build_rank_net(self):
        with tf.variable_scope('ranking_net'):
            n_data = tf.shape(self.source_labels)[0]

            s_ij = self.source_labels - tf.transpose(self.source_labels)
            real_score = tf.cast(s_ij > 0, dtype=tf.float32)

            pairwise_score = self.pred - tf.transpose(self.pred)
            self.rank_loss = tf.reduce_mean(
                (tf.ones([n_data, n_data]) - tf.diag(tf.ones([n_data]))) * tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=pairwise_score, labels=real_score)
            )

    # def _build_rank_net(self):
    #     with tf.variable_scope('ranking_net'):
    #         pred_o1 = tf.cond(
    #             self.dann,
    #             lambda: tf.slice(self.pred, [0, 0], [int(self.batch_size / 4), -1]),
    #             lambda: tf.slice(self.pred, [0, 0], [int(self.batch_size / 2), -1])
    #         )
    #
    #         pred_o2 = tf.cond(
    #             self.dann,
    #             lambda: tf.slice(self.pred, [int(self.batch_size / 4), 0], [int(self.batch_size / 4), -1]),
    #             lambda: tf.slice(self.pred, [int(self.batch_size / 2), 0], [int(self.batch_size / 2), -1])
    #         )
    #
    #         if self.rank_loss_type == 'sigmoid':
    #             r_logits = tf.constant(10.0) * (pred_o1 - pred_o2)
    #             # margin_prob = tf.nn.sigmoid(r_logits)
    #             # self.rank_loss = -tf.reduce_mean(
    #             #     self.y_rank * tf.log(margin_prob) + (1 - self.y_rank) * tf.log(1 - margin_prob))
    #             self.rank_loss = tf.reduce_mean(
    #                 tf.nn.sigmoid_cross_entropy_with_logits(logits=r_logits, labels=self.y_rank), name='rank_loss')
    #         else:
    #             margin = tf.constant(-1.0) * (pred_o1 - pred_o2)
    #             self.rank_loss = tf.reduce_mean(tf.maximum(tf.constant(0.0), margin))

    def build_model(self):
        tf.set_random_seed(1)
        # init variable
        self.init_variable()

        # The domain-invariant feature
        self.feature = self._build_feature_net()

        # MLP for class prediction
        self.pred = self._build_predictor_net()
        self.build_pred_loss(self.pred)

        # Pair-wise ranking loss
        self._build_rank_net()

        # Small MLP for domain prediction with adversarial loss
        self._build_domain_net()

        if self.alpha_mode == 'dynamic':
            self.regular_loss = self.pred_loss + self.alpha * self.rank_loss
        else:
            if self.init_alpha > 0:
                self.regular_loss = (1 - self.init_alpha) * self.pred_loss + self.init_alpha * self.rank_loss
            else:
                self.regular_loss = self.pred_loss

        self.total_loss = self.pred_loss + self.beta * self.domain_loss

        # optimizer
        if self.optimizer == 'adam':
            self.regular_train_op = tf.train.AdamOptimizer(self.init_learning_rate).minimize(self.regular_loss)
            self.dann_train_op = tf.train.AdamOptimizer(self.init_learning_rate).minimize(self.total_loss)
        else:
            self.regular_train_op = tf.train.GradientDescentOptimizer(self.learning_rate).minimize(self.regular_loss)
            self.dann_train_op = tf.train.GradientDescentOptimizer(self.learning_rate).minimize(self.total_loss)

        # acc evaluate
        correct_domain_pred = tf.equal(self.domain, tf.round(self.domain_pred))
        self.domain_acc = tf.reduce_mean(tf.cast(correct_domain_pred, tf.float32))

        self.label_acc = self.regular_loss
        # if self.task_type == 'cls':
        #     self.label_acc = self.pred_loss
        #     # correct_label_pred = tf.equal(self.source_labels, tf.round(self.pred))
        #     # self.label_acc = tf.reduce_mean(tf.cast(correct_label_pred, tf.float32))
        # else:
        #     self.label_acc = tf.sqrt(self.pred_loss)


class RankSolver(Solver):
    def __init__(self, sess, model):
        super(RankSolver, self).__init__(sess, model)

    def train_rank(self, x_c, y, y_rank, l, lr, alpha):
        """
        batch training on source or target
        :return:
        """
        _, batch_loss = self.sess.run(
            [self.model.regular_train_op, self.model.pred_loss],
            feed_dict={
                self.model.X: x_c, self.model.y: y, self.model.y_rank: y_rank,
                self.model.dann: False, self.model.train: True,
                self.model.l: l,
                self.model.learning_rate: lr,
                self.model.alpha: alpha
            }
        )
        return batch_loss

    def train_dann_rank(self, x_c, y, y_rank, domain_labels, l, lr, alpha):
        """
        batch training on dann mode
        :return:
        """
        feed_dict = {
            self.model.X: x_c, self.model.y: y, self.model.y_rank: y_rank, self.model.domain: domain_labels,
            self.model.dann: True, self.model.train: True, self.model.l: l, self.model.learning_rate: lr,
            self.model.alpha: alpha
        }

        _, batch_loss, domain_loss, pred_loss, d_acc, p_acc = self.sess.run(
            [
                self.model.dann_train_op, self.model.total_loss, self.model.domain_loss,
                self.model.pred_loss, self.model.domain_acc, self.model.label_acc
            ],
            feed_dict=feed_dict
        )

        return batch_loss, domain_loss, pred_loss, d_acc, p_acc


def train_and_evaluate_rank_reg(training_mode, graph, model, x_train, y_train, x_val, y_val, x_test, y_test,
                                combine_x, combine_d, verbose=False, batch_size=128, num_steps=4000,
                                mode='mlp_pair_rank',
                                early_stop=True):
    """
    Helper to run the model with different training modes.
    """
    verbose_time = 100
    evaluate_time = 100
    config = tf.ConfigProto()
    config.gpu_options.per_process_gpu_memory_fraction = 0.3
    config.gpu_options.allow_growth = True
    source_losses = []
    val_losses = []
    target_losses = []

    best_val_losses = 1000.0
    early_stop_flag = 0

    saver = tf.train.Saver(max_to_keep=1)
    check_point_path = os.path.join(
        '../check_point', '%s_%s' % (mode, datetime.datetime.now().strftime('%m%d_%H%M%S')))
    if not os.path.exists(check_point_path):
        os.mkdir(check_point_path)
    save_path = os.path.join(check_point_path, 'model.ckpt')

    with tf.Session(config=config, graph=graph) as sess:
        sess.run(tf.global_variables_initializer())
        solver = RankSolver(sess=sess, model=model)

        # Batch generators
        gen_source_batch = batch_generator(
            [x_train, y_train], batch_size // 2)
        gen_target_batch = batch_generator(
            [x_test, y_test], batch_size // 2)
        gen_source_only_batch = batch_generator(
            [x_train, y_train], batch_size)
        gen_target_only_batch = batch_generator(
            [x_test, y_test], batch_size)

        domain_labels = np.vstack([np.zeros((batch_size // 2, 1)),
                                   np.ones((batch_size // 2, 1))])

        # Training loop
        for i in range(num_steps):

            # Adaptation param and learning rate schedule as described in the paper
            p = float(i) / num_steps
            l = 2. / (1. + np.exp(-10. * p)) - 1
            alpha = 2. / (1. + np.exp(-10. * p)) - 1

            lr = 0.0001  # learning rate decay

            # Training step
            if training_mode == 'dann':
                x_s, y_s = next(gen_source_batch)
                x_t, y_t = next(gen_target_batch)
                y_rank = (y_s[:len(y_s) // 2] > y_s[len(y_s) // 2:]).astype('int')

                x_c = np.vstack([x_s, x_t])
                y = np.vstack([y_s, y_t])

                batch_loss, domain_loss, pred_loss, d_acc, p_acc = solver.train_dann_rank(
                    x_c, y, y_rank, domain_labels, l, lr, alpha)

                if verbose and i % verbose_time == 0:
                    logger.info('step: %d, loss: %f  d_acc: %f  p_acc: %f  p: %f  l: %f  lr: %f' % (
                        i, batch_loss, d_acc, p_acc, p, l, lr))

            elif training_mode == 'source':
                x_c, y = next(gen_source_only_batch)
                y_rank = (y[:len(y) // 2] > y[len(y) // 2:]).astype('int')

                batch_loss = solver.train_rank(x_c, y, y_rank, l, lr, alpha)
                if verbose and i % verbose_time == 0:
                    logger.info('step: %d, source loss: %f' % (i, batch_loss))

            elif training_mode == 'target':
                x_c, y = next(gen_target_only_batch)
                y_rank = (y[:len(y) // 2] > y[len(y) // 2:]).astype('int')

                batch_loss = solver.train_rank(x_c, y, y_rank, l, lr, alpha)
                if verbose and i % verbose_time == 0:
                    logger.info('step: %d, target loss: %f' % (i, batch_loss))

            if i % evaluate_time == 0:
                # Compute final evaluation on test data
                source_acc, _ = solver.evaluate(x_train, y_train, batch_size)
                val_acc, _ = solver.evaluate(x_val, y_val, batch_size)
                target_acc, _ = solver.evaluate(x_test, y_test, batch_size)
                test_domain_acc = solver.evaluate_domain(combine_x, combine_d)
                logger.info(
                    'step: %s, source train metric: %f, source val metric: %f, target metric: %f, domain acc: %f' % (
                        i, source_acc, val_acc, target_acc, test_domain_acc)
                )

                if val_acc < best_val_losses:
                    logger.info('Update the model, last best loss: %f, current best loss: %f' % (
                        best_val_losses, val_acc))
                    save_path = saver.save(sess, save_path)
                    best_val_losses = val_acc
                    early_stop_flag = 0
                else:
                    early_stop_flag += 1
                    if early_stop and early_stop_flag > 10:
                        break

                source_losses.append(source_acc)
                val_losses.append(val_acc)
                target_losses.append(target_acc)

        saver.restore(solver.sess, save_path)
        source_acc, source_pred = solver.evaluate(x_train, y_train, batch_size)
        val_acc, _ = solver.evaluate(x_val, y_val, batch_size)
        target_acc, target_pred = solver.evaluate(x_test, y_test, batch_size)
        test_domain_acc = solver.evaluate_domain(combine_x, combine_d)
        test_emb = sess.run(model.feature, feed_dict={model.X: combine_x, model.dann: False, model.train: False})

        source_losses.append(source_acc)
        val_losses.append(val_acc)
        target_losses.append(target_acc)
        plot_losses(source_losses, target_losses, os.path.join(
            '../results', 'dann_' + training_mode + '_rank_loss.png'))

        del saver

    return source_acc, val_acc, target_acc, test_domain_acc, test_emb, source_pred, target_pred, save_path


def run_rank_reg(x_source, y_source, x_target, y_target, city_pair, train_mode='source', bs=128, train_num_steps=9001,
                 init_learning_rate=0.001, percent=90, init_alpha=0.5, beta=1, feature_dropout=0.2,
                 rank_loss_type='max', alpha_mode='dynamic', task_type='reg', pos_weight=1, hot_count=100):
    x_combine, y_combine, d_combine = generate_combine_data(x_source, y_source, x_target, y_target)
    x_train, x_val, y_train, y_val = train_test_split(x_source, y_source, test_size=0.1, random_state=42)

    dann_model = DaCityRankingModel(input_dim=x_source.shape[-1],
                                    init_learning_rate=init_learning_rate, optimizer='adam',
                                    init_alpha=init_alpha, beta=beta,
                                    tensor_board=False, batch_size=bs, rank_loss_type=rank_loss_type,
                                    use_batch_norm=True, feature_dropout=feature_dropout,
                                    predictor_dropout=0.2, alpha_mode=alpha_mode, task_type=task_type,
                                    pos_weight=pos_weight)
    tf.reset_default_graph()
    graph = tf.get_default_graph()
    dann_model.build_model()

    print('\n%s only training' % train_mode)
    source_metric, val_metric, target_metric, domain_metric, feature_embedding, source_pred, target_pred, model_path = \
        train_and_evaluate_rank_reg(
            train_mode, graph, dann_model, x_train, y_train,
            x_val, y_val, x_target, y_target, x_combine, d_combine, verbose=False, batch_size=bs,
            num_steps=train_num_steps,
            mode='mlp_rank_reg_%s' % train_mode
        )

    rank_metrics = dann_evaluate(city_pair, task_type, source_metric, target_metric, domain_metric,
                                 y_target, target_pred, percent, hot_count=hot_count)
    return rank_metrics, val_metric, model_path, feature_embedding


def run_rank_reg_one_time(train_mode='source', path_pattern='../data/road/train_bound_unique_new/%s_500_week.csv',
                          task_type='reg', percent=90, hot_count=100, city_pair=('bj', 'nb'), max_train_steps=10001,
                          save_embedding=False):
    bs = 128
    init_learning_rate = 0.001
    init_alpha = 0.8
    feature_dropout = 0.0

    x_source, y_source, x_target, y_target = load_city_pair_data(
        city_pair[0], city_pair[1], path_pattern=path_pattern, percent=percent, task_type=task_type)

    rank_metrics, _, _, embedding = run_rank_reg(
        x_source, y_source, x_target, y_target, city_pair,
        train_mode=train_mode,
        train_num_steps=max_train_steps, task_type=task_type,
        hot_count=hot_count, bs=bs,
        init_learning_rate=init_learning_rate, alpha_mode='static',
        init_alpha=init_alpha,
        feature_dropout=feature_dropout
    )
    if save_embedding:
        np.save('../data/embedding/%s_%s.npy' % ('_'.join(city_pair), train_mode), embedding)
    print(str(rank_metrics))
    return rank_metrics


def grid_search_rank_reg(train_mode='source', search_mode='grid', repeat=4, city_pair=('bj', 'nb'),
                         data_version='bound_unique_new', task_type='reg', hot_count=100, max_train_steps=10001):
    percent = 90

    x_source, y_source, x_target, y_target = load_city_pair_data(
        city_pair[0], city_pair[1],
        path_pattern='../data/road/train_' + data_version + '/%s_500_week.csv',
        percent=percent, task_type=task_type
    )

    save_dir = '../model_search_res/%s/' % data_version
    if not os.path.exists(save_dir):
        os.mkdir(save_dir)

    gird_search_save_path = '../model_search_res/%s/%s_mlp_rank_reg_cv_%s_%s_%s.csv' % (
        data_version, '_'.join(city_pair), train_mode, task_type, hot_count)

    best_val_metric = 1000.0
    best_model_path = None
    best_param = None

    param_grid = dict(
        bs=[128, ],
        init_learning_rate=[0.001, 0.0001],
        feature_dropout=[0.0, 0.2, 0.4],
        rank_loss_type=['sigmoid'],
        init_alpha=[0, 0.3, 0.5, 0.8, 1.0]
    )

    if train_mode == 'dann':
        param_grid['beta'] = [0.01, 0.1, 1]
    else:
        param_grid['beta'] = [1]

    if task_type == 'cls':
        param_grid['pos_weight'] = [1, 3, 5]

    if search_mode == 'grid':
        candidate_params = list(ParameterGrid(param_grid))
    else:
        candidate_params = list(ParameterSampler(param_grid, n_iter=30))

    search_res_list = []
    for param in candidate_params:
        logger.info(str(param))
        for i in range(repeat):
            run_dict = param.copy()
            run_dict['repeat'] = i
            rank_metrics, val_metric, model_path, _ = run_rank_reg(
                x_source, y_source, x_target, y_target, city_pair, train_mode=train_mode,
                task_type=task_type, train_num_steps=max_train_steps, hot_count=hot_count, alpha_mode='static',
                **param
            )
            run_dict.update(rank_metrics)

            if val_metric < best_val_metric:
                logger.info('Choice new parameter, last best val metric: %f, new best metric: %f' % (
                    best_val_metric, val_metric))
                best_val_metric = val_metric
                best_model_path = model_path
                best_param = param

            search_res_list.append(run_dict)
            logger.info(str(run_dict))

            if task_type == 'reg':
                save_grid_search_res(
                    gird_search_save_path,
                    search_res_list,
                    columns=['beta', 'init_alpha', 'rank_loss_type', 'init_learning_rate', 'bs', 'feature_dropout',
                             'repeat', 'rmse',
                             'map@10', 'ndcg@10', 'map@30', 'ndcg@30', 'map@50', 'ndcg@50']
                )
            else:
                save_grid_search_res(
                    gird_search_save_path,
                    search_res_list,
                    columns=['pos_weight', 'beta', 'init_alpha', 'rank_loss_type', 'init_learning_rate', 'bs',
                             'feature_dropout',
                             'repeat', 'precision', 'recall', 'acc'],
                    sort_col='precision'
                )

    if best_model_path is not None:
        model = DaCityRankingModel(input_dim=x_target.shape[-1], task_type=task_type, alpha_mode='static')
        rank_metrics = evaluate(model, RankSolver, best_model_path, x_target, y_target)
        logger.info('Best parameter: ' + str(best_param))
        logger.info('Best metric:' + str(rank_metrics))


if __name__ == '__main__':
    run_rank_reg_one_time(train_mode='source', city_pair=('sh', 'nb'),
                          path_pattern='../data/road/train_bound%s_500_week.csv',
                          task_type='reg', hot_count=100, save_embedding=False)
    run_rank_reg_one_time(train_mode='dann', city_pair=('sh', 'nb'),
                          path_pattern='../data/road/train_bound/%s_500_week.csv',
                          task_type='reg', hot_count=100, save_embedding=False)

    grid_search_rank_reg(train_mode='source', search_mode='grid', repeat=1, city_pair=('sh', 'nb'),
                         data_version='bound', task_type='reg', hot_count=100)
    grid_search_rank_reg(train_mode='dann', search_mode='grid', repeat=1, city_pair=('sh', 'nb'),
                         data_version='bound', task_type='reg', hot_count=100)
