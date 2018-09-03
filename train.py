# -*- coding: utf-8 -*-
"""
Python File Template 
"""
import json
import os
import sys
import argparse

import logging
import numpy as np
import time
import torchtext
from torch.autograd import Variable
from torch.optim import Adam
from torch.utils.data import DataLoader

import config
import evaluate
import utils
import copy

import torch
import torch.nn as nn
from torch import cuda

from beam_search import SequenceGenerator
from evaluate import evaluate_beam_search, get_match_result, self_redundancy
from pykp.dataloader import KeyphraseDataLoader
from utils import Progbar, plot_learning_curve_and_write_csv

import pykp
from pykp.io import KeyphraseDataset
from pykp.model import Seq2SeqLSTMAttention, Seq2SeqLSTMAttentionCascading

import torch.nn.parallel.data_parallel

import time


def to_cpu_list(input):
    assert isinstance(input, list)
    output = [int(item.data.cpu().numpy()) for item in input]
    return output

__author__ = "Rui Meng"
__email__ = "rui.meng@pitt.edu"


def train_mle(batch_data_dict, model, optimizer, criterion, opt):
    # src, src_len, trg, trg_target, trg_copy_target, src_oov, oov_lists = batch_data_dict
    src = batch_data_dict['src_unk']
    src_copy = batch_data_dict['src_copy']
    src_len = batch_data_dict['src_len']

    trg = batch_data_dict['trg_unk']
    trg_len = batch_data_dict['trg_len']
    trg_mask = batch_data_dict['trg_mask']
    trg_unk_for_loss = batch_data_dict['trg_unk_for_loss']
    trg_copy_for_loss = batch_data_dict['trg_copy_for_loss']
    oov_numbers = [len(oov_list) for oov_list in batch_data_dict['oov_lists']]
    max_oov_number = max(oov_numbers)

    src_len = Variable(torch.from_numpy(np.asarray(src_len))).long()
    oov_numbers = Variable(torch.from_numpy(np.asarray(oov_numbers))).long()

    if torch.cuda.is_available():
        if len(opt.device_ids) == 1:
            src = src.cuda()
            trg = trg.cuda()
            trg_copy_for_loss = trg_copy_for_loss.cuda()
            src_copy = src_copy.cuda()
            src_len = src_len.cuda()
            oov_numbers = oov_numbers.cuda()

        trg_unk_for_loss = trg_unk_for_loss.cuda(opt.device_ids[0])

    start_time = time.time()
    optimizer.zero_grad()
    decoder_log_probs, _, _ = model.forward(src, src_len, trg, trg_len, src_copy, oov_numbers)

    print("Outside Model: input src size", src.size(),
          "output decoder_logits size", decoder_log_probs.size())

    if not opt.copy_attention:
        loss = criterion(
            decoder_log_probs.contiguous().view(-1, opt.vocab_size),
            trg_unk_for_loss.contiguous().view(-1)
        )
    else:
        loss = criterion(
            decoder_log_probs.contiguous().view(-1, opt.vocab_size + max_oov_number),
            trg_copy_for_loss.contiguous().view(-1)
        )

    if opt.train_rl:
        loss = loss * (1 - opt.loss_scale)

    logging.info('src.shape=%s' % str(src.shape))
    logging.info('trg.shape=%s' % str(trg.shape))

    logging.info("--forward+loss- %s seconds ---" % (time.time() - start_time))

    start_time = time.time()
    loss.backward()
    optimizer.step()
    logging.info("--backward- %s seconds ---" % (time.time() - start_time))

    if opt.max_grad_norm > 0:
        pre_norm = torch.nn.utils.clip_grad_norm(model.parameters(), opt.max_grad_norm)
        after_norm = (sum([p.grad.data.norm(2) ** 2 for p in model.parameters() if p.grad is not None])) ** (1.0 / 2)
        logging.info('clip grad (%f -> %f)' % (pre_norm, after_norm))

    if torch.cuda.is_available():
        loss = loss.cpu().data.numpy()
    else:
        loss = loss.data.numpy()

    return loss, decoder_log_probs


def train_rl_0(one2many_batch, model, optimizer, generator, opt):
    src_list, src_len, trg_list, _, trg_copy_target_list, src_oov_map_list, oov_list = one2many_batch

    if torch.cuda.is_available():
        src_list = src_list.cuda()
        src_oov_map_list = src_oov_map_list.cuda()

    # Baseline sequences for self-critic
    baseline_seqs_list = generator.sample(src_list, src_len, src_oov_map_list, oov_list, opt.word2id, k=5, is_greedy=True)

    # Sample number_batch*beam_size sequences
    sampled_seqs_list = generator.sample(src_list, src_len, src_oov_map_list, oov_list, opt.word2id, k=5, is_greedy=False)

    policy_loss = []
    policy_rewards = []
    # Compute their rewards and losses
    for seq_i, (src, trg, trg_copy, sampled_seqs, baseline_seqs, oov) in enumerate(zip(src_list, trg_list, trg_copy_target_list, sampled_seqs_list, baseline_seqs_list, oov_list)):
        # convert to string sequences
        baseline_str_seqs = [[opt.id2word[x] if x < opt.vocab_size else oov[x - opt.vocab_size] for x in to_cpu_list(seq.sentence)] for seq in baseline_seqs]
        baseline_str_seqs = [seq[:seq.index(pykp.io.EOS_WORD) + 1] if pykp.io.EOS_WORD in seq else seq for seq in baseline_str_seqs]
        sampled_str_seqs = [[opt.id2word[x] if x < opt.vocab_size else oov[x - opt.vocab_size] for x in to_cpu_list(seq.sentence)] for seq in sampled_seqs]
        sampled_str_seqs = [seq[:seq.index(pykp.io.EOS_WORD) + 1] if pykp.io.EOS_WORD in seq else seq for seq in sampled_str_seqs]

        # pad trg seqs with EOS to the same length
        trg_seqs = [[opt.id2word[x] if x < opt.vocab_size else oov[x - opt.vocab_size] for x in seq] for seq in trg_copy]
        # trg_seqs            =  [seq + [pykp.IO.EOS_WORD] * (opt.beam_search_max_length - len(seq)) for seq in trg_seqs]

        # local rewards (bleu)
        bleu_baselines = get_match_result(true_seqs=trg_seqs, pred_seqs=baseline_str_seqs, type='bleu')
        bleu_samples = get_match_result(true_seqs=trg_seqs, pred_seqs=sampled_str_seqs, type='bleu')

        # global rewards
        match_baselines = get_match_result(true_seqs=trg_seqs, pred_seqs=baseline_str_seqs, type='exact')
        match_samples = get_match_result(true_seqs=trg_seqs, pred_seqs=sampled_str_seqs, type='exact')

        _, _, fscore_baselines = evaluate.evaluate(match_baselines, baseline_str_seqs, trg_seqs, topk=5)
        _, _, fscore_samples = evaluate.evaluate(match_samples, sampled_str_seqs, trg_seqs, topk=5)

        # compute the final rewards
        alpha = 0.0
        baseline = alpha * np.average(bleu_baselines) + (1.0 - alpha) * fscore_baselines
        rewards = alpha * np.asarray(bleu_samples) + (1.0 - alpha) * fscore_samples

        """
        print('*' * 20 + '  ' + str(seq_i) + '  ' + '*' * 20)
        print('Target Sequences:\n\t\t %s' % str(trg_seqs))
        print('Baseline Sequences:')
        for pred_seq, reward in zip(baseline_str_seqs, baselines):
            print('\t\t[%f] %s' % (reward, ' '.join(pred_seq)))
        print('Predict Sequences:')
        for pred_seq, reward in zip(sampled_str_seqs, rewards):
            print('\t\t[%f] %s' % (reward, ' '.join(pred_seq)))
        """

        [policy_loss.append(-torch.stack(seq.logprobs, dim=0) * float(reward - baseline)) for seq, reward in zip(sampled_seqs, rewards)]
        [policy_rewards.append(reward) for reward in rewards]

    optimizer.zero_grad()
    policy_loss = torch.cat(policy_loss).sum() * (1 - opt.loss_scale)
    policy_loss.backward()

    if opt.max_grad_norm > 0:
        pre_norm = torch.nn.utils.clip_grad_norm(model.parameters(), opt.max_grad_norm)
        after_norm = (sum([p.grad.data.norm(2) ** 2 for p in model.parameters() if p.grad is not None])) ** (1.0 / 2)
        # logging.info('clip grad (%f -> %f)' % (pre_norm, after_norm))

    optimizer.step()
    return np.average(policy_rewards)


class RewardCache(object):

    def __init__(self, capacity=2000):
        # vanilla replay memory
        self.capacity = capacity
        self.memory = []
        self.reset()

    def push(self, stuff):
        if len(self.memory) == self.capacity:
            self.memory = self.memory[1:]
        self.memory.append(stuff)

    def get_average(self):
        if len(self.memory) == 0:
            return 0
        return np.mean(np.array(self.memory))

    def reset(self):
        self.memory = []

    def __len__(self):
        return len(self.memory)


def train_rl_1(one2many_batch, model, optimizer, generator, opt, reward_cache):
    src_list, src_len, trg_list, _, trg_copy_target_list, src_oov_map_list, oov_list = one2many_batch

    if torch.cuda.is_available():
        src_list = src_list.cuda()
        src_oov_map_list = src_oov_map_list.cuda()

    # Sample number_batch sequences
    sampled_seqs_list = generator.sample(src_list, src_len, src_oov_map_list, oov_list, opt.word2id, k=5, is_greedy=False)

    policy_loss = []
    policy_rewards = []
    # Compute their rewards and losses
    for seq_i, (src, trg, trg_copy, sampled_seqs, oov) in enumerate(zip(src_list, trg_list, trg_copy_target_list, sampled_seqs_list, oov_list)):
        # convert to string sequences
        sampled_str_seqs = [[opt.id2word[x] if x < opt.vocab_size else oov[x - opt.vocab_size] for x in to_cpu_list(seq.sentence)] for seq in sampled_seqs]
        sampled_str_seqs = [seq[:seq.index(pykp.io.EOS_WORD) + 1] if pykp.io.EOS_WORD in seq else seq for seq in sampled_str_seqs]

        # pad trg seqs with EOS to the same length
        trg_seqs = [[opt.id2word[x] if x < opt.vocab_size else oov[x - opt.vocab_size] for x in seq] for seq in trg_copy]
        # trg_seqs            =  [seq + [pykp.IO.EOS_WORD] * (opt.beam_search_max_length - len(seq)) for seq in trg_seqs]

        # local rewards (bleu)
        bleu_samples = get_match_result(true_seqs=trg_seqs, pred_seqs=sampled_str_seqs, type='bleu')

        # global rewards
        match_samples = get_match_result(true_seqs=trg_seqs, pred_seqs=sampled_str_seqs, type='exact')

        _, _, fscore_samples = evaluate.evaluate(match_samples, sampled_str_seqs, trg_seqs, topk=5)

        # compute the final rewards
        alpha = 0.0
        rewards = alpha * np.asarray(bleu_samples) + (1.0 - alpha) * fscore_samples
        baseline = reward_cache.get_average()
        for reward in rewards:
            reward_cache.push(float(reward))

        [policy_loss.append(-torch.stack(seq.logprobs, dim=0).sum() * float(reward - baseline)) for seq, reward in zip(sampled_seqs, rewards)]
        [policy_rewards.append(reward) for reward in rewards]

    optimizer.zero_grad()
    policy_loss = torch.stack(policy_loss).mean() * (1 - opt.loss_scale)
    policy_loss.backward()

    if opt.max_grad_norm > 0:
        pre_norm = torch.nn.utils.clip_grad_norm(model.parameters(), opt.max_grad_norm)
        after_norm = (sum([p.grad.data.norm(2) ** 2 for p in model.parameters() if p.grad is not None])) ** (1.0 / 2)
        # logging.info('clip grad (%f -> %f)' % (pre_norm, after_norm))

    optimizer.step()
    return np.average(policy_rewards)


def train_rl_2(one2many_batch, model, optimizer, generator, opt, reward_cache):
    src_list, src_len, trg_list, _, trg_copy_target_list, src_oov_map_list, oov_list = one2many_batch

    if torch.cuda.is_available():
        src_list = src_list.cuda()
        src_oov_map_list = src_oov_map_list.cuda()

    # Sample number_batch sequences
    sampled_seqs_list = generator.sample(src_list, src_len, src_oov_map_list, oov_list, opt.word2id, k=5, is_greedy=False)

    policy_loss = []
    policy_rewards = []
    # Compute their rewards and losses
    for seq_i, (src, trg, trg_copy, sampled_seqs, oov) in enumerate(zip(src_list, trg_list, trg_copy_target_list, sampled_seqs_list, oov_list)):
        # convert to string sequences
        sampled_str_seqs = [[opt.id2word[x] if x < opt.vocab_size else oov[x - opt.vocab_size] for x in to_cpu_list(seq.sentence)] for seq in sampled_seqs]
        sampled_str_seqs = [seq[:seq.index(pykp.io.EOS_WORD) + 1] if pykp.io.EOS_WORD in seq else seq for seq in sampled_str_seqs]

        redundancy = self_redundancy(sampled_str_seqs)
        reward = 1.0 - redundancy  # the less redundant, the better

        baseline = reward_cache.get_average()
        reward_cache.push(float(reward))

        [policy_loss.append(-torch.stack(seq.logprobs, dim=0).sum() * float(reward - baseline)) for seq in sampled_seqs]
        policy_rewards.append(reward)

    optimizer.zero_grad()
    policy_loss = torch.stack(policy_loss).mean() * (1 - opt.loss_scale)
    policy_loss.backward()

    if opt.max_grad_norm > 0:
        pre_norm = torch.nn.utils.clip_grad_norm(model.parameters(), opt.max_grad_norm)
        after_norm = (sum([p.grad.data.norm(2) ** 2 for p in model.parameters() if p.grad is not None])) ** (1.0 / 2)
        # logging.info('clip grad (%f -> %f)' % (pre_norm, after_norm))

    optimizer.step()
    return np.average(policy_rewards)


def train_rl(one2many_batch, model, optimizer, generator, opt, reward_cache):
    if opt.rl_method == 0:
        return train_rl_0(one2many_batch, model, optimizer, generator, opt)
    elif opt.rl_method == 1:
        return train_rl_1(one2many_batch, model, optimizer, generator, opt, reward_cache)
    elif opt.rl_method == 2:
        return train_rl_2(one2many_batch, model, optimizer, generator, opt, reward_cache)


def brief_report(epoch, batch_i, one2many_batch, one2one_batch, loss_ml, decoder_log_probs, opt):
    """
    Given many examples in one2one_batch and the predicted decoder_log_probs,
    we randomly select a few examples and print their corresponding outputs
    :param epoch:
    :param batch_i:
    :param one2one_batch:
    :param loss_ml:
    :param decoder_log_probs:
            if cascading: (batch_size * trg_num, trg_max_len, vocab_size)
                else: (batch_size, trg_max_len, vocab_size)
    :param opt:
    :return:
    """
    logging.info('======================  %d  =========================' % (batch_i))

    logging.info('Epoch : %d Minibatch : %d, Loss=%.5f' % (epoch, batch_i, np.mean(loss_ml)))
    sampled_size = 2
    logging.info('Printing predictions on %d sampled examples by greedy search' % sampled_size)

    if opt.cascading_model:
        src_num = one2many_batch['src_unk'].shape[0]
        trg_max_num = one2many_batch['trg_unk_for_loss'].shape[1]
        trg_max_len = one2many_batch['trg_unk_for_loss'].shape[2]
        src = one2many_batch['src_unk']
        trg_unk_for_loss = one2many_batch['trg_unk_for_loss']
        trg_copy_for_loss = one2many_batch['trg_copy_for_loss']
        oov_lists = one2many_batch['oov_lists']

        if torch.cuda.is_available():
            src = src.cpu()
            decoder_log_probs = decoder_log_probs.cpu()
            trg_unk_for_loss = trg_unk_for_loss.cpu()
            trg_copy_for_loss = trg_copy_for_loss.cpu()

        src = src.data.numpy()
        # (batch_size, trg_max_len, voc_size)
        decoder_log_probs = decoder_log_probs.data.numpy()
        # Greedily get the top-1 word at each step as prediction (batch_size * trg_num, trg_max_len)
        argmax_pred_trgs = decoder_log_probs.argmax(axis=-1)
        trg_unk_for_loss = trg_unk_for_loss.view(-1, trg_max_len).data.numpy()
        trg_copy_for_loss = trg_copy_for_loss.view(-1, trg_max_len).data.numpy()

        # sample a few indices of examples and print the first prediction of each
        sampled_src_idx = np.random.permutation(src_num - 1)[:sampled_size]
        # sampled_src_idx = np.random.random_integers(low=0, high=src_num - 1, size=sampled_size)
        src = src[sampled_src_idx]
        oov_lists = [oov_lists[i] for i in sampled_src_idx]
        argmax_pred_trgs = [argmax_pred_trgs[i * trg_max_num] for i in sampled_src_idx]
        decoder_log_probs = [decoder_log_probs[i * trg_max_num] for i in sampled_src_idx]

        if not opt.copy_attention:
            # use the real target trg_loss (the starting <BOS> has been removed and contains oov ground-truth)
            trgs = [trg_unk_for_loss[i * trg_max_num] for i in sampled_src_idx]
        else:
            trgs = [trg_copy_for_loss[i * trg_max_num] for i in sampled_src_idx]
    else:
        src = one2one_batch['src_unk']
        trg_unk_for_loss = one2one_batch['trg_unk_for_loss']
        example_num = one2one_batch['trg_unk_for_loss'].shape[0]
        trg_copy_for_loss = one2one_batch['trg_copy_for_loss']
        oov_lists = one2one_batch['oov_lists']

        if torch.cuda.is_available():
            src = src.cpu()
            decoder_log_probs = decoder_log_probs.cpu()
            trg_unk_for_loss = trg_unk_for_loss.cpu()
            trg_copy_for_loss = trg_copy_for_loss.cpu()

        src = src.data.numpy()
        # (batch_size, trg_max_len, voc_size)
        decoder_log_probs = decoder_log_probs.data.numpy()
        # Greedily get the top-1 word at each step as prediction (batch_size, trg_max_len)
        argmax_pred_trgs = decoder_log_probs.argmax(axis=-1)
        trg_unk_for_loss = trg_unk_for_loss.data.numpy()
        trg_copy_for_loss = trg_copy_for_loss.data.numpy()

        # sample a few indices of targets to print
        sampled_trg_idx = np.random.permutation(example_num - 1)[:sampled_size]
        # sampled_trg_idx = np.random.random_integers(low=0, high=len(trg_unk_for_loss) - 1, size=sampled_size)
        src = src[sampled_trg_idx]
        oov_lists = [oov_lists[i] for i in sampled_trg_idx]
        argmax_pred_trgs = [argmax_pred_trgs[i] for i in sampled_trg_idx]
        decoder_log_probs = decoder_log_probs[sampled_trg_idx]

        if not opt.copy_attention:
            # use the real target trg_loss (the starting <BOS> has been removed and contains oov ground-truth)
            trgs = [trg_unk_for_loss[i] for i in sampled_trg_idx]
        else:
            trgs = [trg_copy_for_loss[i] for i in sampled_trg_idx]

    try:
        for i, (src_i, pred_i, trg_i, oov_i) in enumerate(
                zip(src, argmax_pred_trgs, trgs, oov_lists)):
            nll_prob = -np.sum([decoder_log_probs[i][l][pred_i[l]] for l in range(len(trg_i))])
            find_copy = np.any([x >= opt.vocab_size for x in src_i])
            has_copy = np.any([x >= opt.vocab_size for x in trg_i])

            sentence_source = [opt.id2word[x] if x < opt.vocab_size else oov_i[x - opt.vocab_size]
                               for x in src_i]
            logging.info('oov list: %s' % str(oov_i))
            logging.info('greedy pred_wi: %s' % str(pred_i))
            sentence_pred = [opt.id2word[x] if x < opt.vocab_size else oov_i[x - opt.vocab_size]
                             for x in pred_i]
            sentence_real = [opt.id2word[x] if x < opt.vocab_size else oov_i[x - opt.vocab_size]
                             for x in trg_i]

            sentence_source = sentence_source[:sentence_source.index(
                '<pad>')] if '<pad>' in sentence_source else sentence_source
            sentence_pred = sentence_pred[
                :sentence_pred.index('<pad>')] if '<pad>' in sentence_pred else sentence_pred
            sentence_real = sentence_real[
                :sentence_real.index('<pad>')] if '<pad>' in sentence_real else sentence_real

            logging.info('==================================================')
            logging.info('Source: %s ' % (' '.join(sentence_source)))
            logging.info('\t\tPred : %s (%.4f)' % (' '.join(sentence_pred), nll_prob) + (
                ' [FIND COPY]' if find_copy else ''))
            logging.info('\t\tReal : %s ' % (' '.join(sentence_real)) + (
                ' [HAS COPY]' + str(trg_i) if has_copy else ''))
    except Exception:
        logging.error('Encountered an error when generating brief report.')
        pass

def train_model(model, optimizer_ml, optimizer_rl, criterion, train_data_loader, valid_data_loader, test_data_loader, opt):
    generator = SequenceGenerator(model,
                                  eos_id=opt.word2id[pykp.io.EOS_WORD],
                                  beam_size=opt.beam_size,
                                  max_sequence_length=opt.beam_search_max_length
                                  )

    logging.info('======================  Start Training  =========================')

    checkpoint_names = []
    train_ml_history_losses = []
    train_rl_history_losses = []
    valid_history_losses = []
    test_history_losses = []
    # best_loss = sys.float_info.max # for normal training/testing loss (likelihood)
    best_loss = 0.0  # for f-score
    stop_increasing = 0

    train_ml_losses = []
    train_rl_losses = []
    total_batch = -1
    early_stop_flag = False
    if opt.train_rl:
        reward_cache = RewardCache(2000)

    if False:  # opt.train_from:
        state_path = opt.train_from.replace('.model', '.state')
        logging.info('Loading training state from: %s' % state_path)
        if os.path.exists(state_path):
            (epoch, total_batch, best_loss, stop_increasing, checkpoint_names, train_ml_history_losses, train_rl_history_losses, valid_history_losses,
             test_history_losses) = torch.load(open(state_path, 'rb'))
            opt.start_epoch = epoch

    for epoch in range(opt.start_epoch, opt.epochs):
        if early_stop_flag:
            break

        progbar = Progbar(logger=logging, title='Training', target=len(train_data_loader), batch_size=train_data_loader.batch_size,
                          total_examples=len(train_data_loader.dataset.examples))

        for batch_i, batch in enumerate(train_data_loader):
            model.train()
            total_batch += 1
            one2many_batch_dict, one2one_batch_dict = batch
            report_loss = []

            # Training
            if opt.train_ml:
                if opt.cascading_model:
                    loss_ml, decoder_log_probs = train_mle(one2many_batch_dict, model, optimizer_ml, criterion, opt)
                else:
                    loss_ml, decoder_log_probs = train_mle(one2one_batch_dict, model, optimizer_ml, criterion, opt)

                train_ml_losses.append(loss_ml)
                report_loss.append(('train_ml_loss', loss_ml))
                report_loss.append(('PPL', loss_ml))

                # Brief report
                if batch_i % opt.report_every == 0:
                    brief_report(epoch, batch_i, one2many_batch_dict, one2one_batch_dict, loss_ml, decoder_log_probs, opt)

            # do not apply rl in the first epoch, need to warm model up with MLE.
            if opt.train_rl:
                if epoch >= opt.rl_start_epoch:
                    loss_rl = train_rl(one2many_batch_dict, model, optimizer_rl, generator, opt, reward_cache)
                else:
                    loss_rl = 0.0
                train_rl_losses.append(loss_rl)
                report_loss.append(('train_rl_loss', loss_rl))

            progbar.update(epoch, batch_i, report_loss)

            # Validate and save checkpoint
            if (opt.run_valid_every == -1 and batch_i == len(train_data_loader) - 1) or\
               (total_batch % opt.run_valid_every == 0 and opt.run_valid_every > -1 and total_batch > 1):
                logging.info('*' * 50)
                logging.info('Run validing and testing @Epoch=%d,#(Total batch)=%d' % (epoch, total_batch))
                # valid_losses    = _valid_error(valid_data_loader, model, criterion, epoch, opt)
                # valid_history_losses.append(valid_losses)
                valid_score_dict = evaluate_beam_search(generator, valid_data_loader, opt,
                                                        title='Validating-epoch=%d-batch=%d-total_batch=%d'
                                                              % (epoch, batch_i, total_batch),
                                                        epoch=epoch,
                                                        predict_save_path=opt.pred_path + '/epoch%d_batch%d_total_batch%d'
                                                                                          % (epoch, batch_i, total_batch))
                test_score_dict = evaluate_beam_search(generator, test_data_loader, opt,
                                                       title='Testing-epoch=%d-batch=%d-total_batch=%d'
                                                             % (epoch, batch_i, total_batch),
                                                       epoch=epoch,
                                                       predict_save_path=opt.pred_path + '/epoch%d_batch%d_total_batch%d'
                                                                                         % (epoch, batch_i, total_batch))

                checkpoint_names.append('epoch=%d-batch=%d-total_batch=%d' % (epoch, batch_i, total_batch))

                curve_names = []
                scores = []
                if opt.train_ml:
                    train_ml_history_losses.append(copy.copy(train_ml_losses))
                    scores += [train_ml_history_losses]
                    curve_names += ['Training ML Error']
                    train_ml_losses = []

                if opt.train_rl:
                    train_rl_history_losses.append(copy.copy(train_rl_losses))
                    scores += [train_rl_history_losses]
                    curve_names += ['Training RL Reward']
                    train_rl_losses = []

                valid_history_losses.append(valid_score_dict)
                test_history_losses.append(test_score_dict)

                scores += [[result_dict[name] for result_dict in valid_history_losses] for name in opt.report_score_names]
                curve_names += ['Valid-' + name for name in opt.report_score_names]
                scores += [[result_dict[name] for result_dict in test_history_losses] for name in opt.report_score_names]
                curve_names += ['Test-' + name for name in opt.report_score_names]

                scores = [np.asarray(s) for s in scores]
                # Plot the learning curve
                plot_learning_curve_and_write_csv(scores=scores,
                                                  curve_names=curve_names,
                                                  checkpoint_names=checkpoint_names,
                                                  title='Training Validation & Test',
                                                  save_path_prefix=opt.plot_path + '/[epoch=%d,batch=%d,total_batch=%d]train_valid_test_curve' % (epoch, batch_i, total_batch))

                '''
                determine if early stop training (whether f-score increased, before is if valid error decreased)
                '''
                valid_loss = np.average(valid_history_losses[-1][opt.report_score_names[0]])
                is_best_loss = valid_loss > best_loss
                rate_of_change = float(valid_loss - best_loss) / float(best_loss) if float(best_loss) > 0 else 0.0

                # valid error doesn't increase
                if rate_of_change <= 0:
                    stop_increasing += 1
                else:
                    stop_increasing = 0

                if is_best_loss:
                    logging.info('Validation: update best loss (%.4f --> %.4f), rate of change (ROC)=%.2f' % (
                        best_loss, valid_loss, rate_of_change * 100))
                else:
                    logging.info('Validation: best loss is not updated for %d times (%.4f --> %.4f), rate of change (ROC)=%.2f' % (
                        stop_increasing, best_loss, valid_loss, rate_of_change * 100))

                best_loss = max(valid_loss, best_loss)

                # only store the checkpoints that make better validation performances
                if total_batch > 1 and (total_batch % opt.save_model_every == 0 or is_best_loss):  # epoch >= opt.start_checkpoint_at and
                    # Save the checkpoint
                    logging.info('Saving checkpoint to: %s' % os.path.join(opt.model_path, '%s.epoch=%d.batch=%d.total_batch=%d.error=%f' % (opt.exp, epoch, batch_i, total_batch, valid_loss) + '.model'))
                    torch.save(
                        model.state_dict(),
                        open(os.path.join(opt.model_path, '%s.epoch=%d.batch=%d.total_batch=%d' % (opt.exp, epoch, batch_i, total_batch) + '.model'), 'wb')
                    )
                    torch.save(
                        (epoch, total_batch, best_loss, stop_increasing, checkpoint_names, train_ml_history_losses, train_rl_history_losses, valid_history_losses, test_history_losses),
                        open(os.path.join(opt.model_path, '%s.epoch=%d.batch=%d.total_batch=%d' % (opt.exp, epoch, batch_i, total_batch) + '.state'), 'wb')
                    )

                if stop_increasing >= opt.early_stop_tolerance:
                    logging.info('Have not increased for %d epoches, early stop training' % stop_increasing)
                    early_stop_flag = True
                    break
                logging.info('*' * 50)


def load_data_vocab(opt, load_train=True):

    logging.info("Loading vocab from disk: %s" % (opt.vocab_file))
    word2id, id2word, vocab = torch.load(opt.vocab_file, 'wb')
    logging.info("Loading data from '%s'" % opt.data_path_prefix)

    count_example_number_by_source = False
    drop_last = False
    fill_up_batch = False

    # if cascading, count source as number pf exmaples
    if opt.cascading_model:
        count_example_number_by_source = True

    # if using more than 1 GPU, use number of source as example count, to make all batches are in equal size
    if torch.cuda.is_available() and len(opt.device_ids) > 1:
        # drop the last batch whose size may not be compatible with multiple-gpu
        drop_last = True
        fill_up_batch = True

    logging.info('======================  Dataset  =========================')
    # one2many data loader
    if load_train:
        train_one2many = torch.load(opt.data_path_prefix + '.train.one2many.pt', 'wb')
        # sort by number of targets to speed up training
        if opt.cascading_model:
            train_one2many = list(filter(lambda x: len(x['trg']) < 10, train_one2many))
            train_one2many = sorted(train_one2many, key=lambda x: len(x['trg']))
        train_one2many_dataset = KeyphraseDataset(train_one2many,
                                                  word2id=word2id, id2word=id2word,
                                                  type='one2many',
                                                  shuffle_targets=True,
                                                  batch_size=opt.batch_size)
        train_one2many_loader = KeyphraseDataLoader(dataset=train_one2many_dataset,
                                                    collate_fn=train_one2many_dataset.collate_fn_one2many,
                                                    num_workers=opt.batch_workers,
                                                    batch_size=opt.batch_size,
                                                    pin_memory=True,
                                                    shuffle=True,
                                                    drop_last=drop_last,
                                                    fill_up_batch=fill_up_batch,
                                                    count_example_number_by_source = count_example_number_by_source)
        logging.info('#(train data size: #(one2many pair)=%d, #(one2one pair)=%d, '
                     '#(batch)=%d,'
                     'avg(one2many/batch)=%.3f, avg(one2one/batch)=%.3f' %
                     (len(train_one2many_loader.dataset), train_one2many_loader.one2one_number(),
                      len(train_one2many_loader),
                      len(train_one2many_loader.dataset) / len(train_one2many_loader),
                      train_one2many_loader.one2one_number() / len(train_one2many_loader)))
    else:
        train_one2many_loader = None

    valid_one2many = torch.load(opt.data_path_prefix + '.valid.one2many.pt', 'wb')
    test_one2many = torch.load(opt.data_path_prefix + '.test.one2many.pt', 'wb')

    # !important. As it takes too long to do beam search, thus reduce the size of validation and test datasets
    valid_one2many = valid_one2many[:2000]
    test_one2many = test_one2many[:2000]

    valid_one2many_dataset = KeyphraseDataset(valid_one2many,
                                              word2id=word2id, id2word=id2word,
                                              type='one2many',
                                              include_original=True,
                                              shuffle_targets=False,
                                              batch_size=opt.beam_search_batch_size)
    test_one2many_dataset = KeyphraseDataset(test_one2many,
                                             word2id=word2id, id2word=id2word,
                                             type='one2many',
                                             include_original=True,
                                             shuffle_targets=False,
                                             batch_size=opt.beam_search_batch_size)

    """
    # temporary code, exporting test data for Theano model
    for e_id, e in enumerate(test_one2many_dataset.examples):
        with open(os.path.join('data', 'new_kp20k_for_theano_model', 'text', '%d.txt' % e_id), 'w') as t_file:
            t_file.write(' '.join(e['src_str']))
        with open(os.path.join('data', 'new_kp20k_for_theano_model', 'keyphrase', '%d.txt' % e_id), 'w') as t_file:
            t_file.writelines([(' '.join(t))+'\n' for t in e['trg_str']])
    exit()
    """

    valid_one2many_loader = KeyphraseDataLoader(dataset=valid_one2many_dataset,
                                                collate_fn=valid_one2many_dataset.collate_fn_one2many,
                                                num_workers=opt.batch_workers,
                                                batch_size=opt.beam_search_batch_size,
                                                pin_memory=True,
                                                shuffle=False,
                                                drop_last=drop_last,
                                                fill_up_batch=fill_up_batch,
                                                count_example_number_by_source = count_example_number_by_source)
    test_one2many_loader = KeyphraseDataLoader(dataset=test_one2many_dataset,
                                               collate_fn=test_one2many_dataset.collate_fn_one2many,
                                               num_workers=opt.batch_workers,
                                               batch_size=opt.beam_search_batch_size,
                                               pin_memory=True,
                                               shuffle=False,
                                               drop_last=drop_last,
                                               fill_up_batch=fill_up_batch,
                                               count_example_number_by_source = count_example_number_by_source)

    opt.word2id = word2id
    opt.id2word = id2word
    opt.vocab = vocab

    logging.info('#(valid data size: #(one2many pair)=%d, #(one2one pair)=%d, #(batch)=%d' % (len(valid_one2many_loader.dataset), valid_one2many_loader.one2one_number(), len(valid_one2many_loader)))
    logging.info('#(test data size:  #(one2many pair)=%d, #(one2one pair)=%d, #(batch)=%d' % (len(test_one2many_loader.dataset), test_one2many_loader.one2one_number(), len(test_one2many_loader)))

    logging.info('#(vocab)=%d' % len(vocab))
    logging.info('#(vocab used)=%d' % opt.vocab_size)

    return train_one2many_loader, valid_one2many_loader, test_one2many_loader, word2id, id2word, vocab


def init_optimizer_criterion(model, opt):
    """
    mask the PAD <pad> when computing loss, before we used weight matrix, but not handy for copy-model, change to ignore_index
    :param model:
    :param opt:
    :return:
    """
    '''
    if not opt.copy_attention:
        weight_mask = torch.ones(opt.vocab_size).cuda() if torch.cuda.is_available() else torch.ones(opt.vocab_size)
    else:
        weight_mask = torch.ones(opt.vocab_size + opt.max_unk_words).cuda() if torch.cuda.is_available() else torch.ones(opt.vocab_size + opt.max_unk_words)
    weight_mask[opt.word2id[pykp.IO.PAD_WORD]] = 0
    criterion = torch.nn.NLLLoss(weight=weight_mask)

    optimizer = Adam(params=filter(lambda p: p.requires_grad, model.parameters()), lr=opt.learning_rate)
    # optimizer = torch.optim.Adadelta(model.parameters(), lr=0.1)
    # optimizer = torch.optim.RMSprop(model.parameters(), lr=0.1)
    '''
    criterion = torch.nn.NLLLoss(ignore_index=opt.word2id[pykp.io.PAD_WORD])

    if opt.train_ml:
        optimizer_ml = Adam(params=filter(lambda p: p.requires_grad, model.parameters()), lr=opt.learning_rate)
    else:
        optimizer_ml = None

    if opt.train_rl:
        optimizer_rl = Adam(params=filter(lambda p: p.requires_grad, model.parameters()), lr=opt.learning_rate_rl)
    else:
        optimizer_rl = None

    if torch.cuda.is_available():
        criterion = criterion.cuda(device=opt.device_ids[0])

    return optimizer_ml, optimizer_rl, criterion


def init_model(opt):
    logging.info('======================  Model Parameters  =========================')

    if opt.cascading_model:
        model = Seq2SeqLSTMAttentionCascading(opt)
    else:
        if opt.copy_attention:
            logging.info('Train a Seq2Seq model with Copy Mechanism')
        else:
            logging.info('Train a normal Seq2Seq model')
        model = Seq2SeqLSTMAttention(opt)

    if opt.train_from:
        logging.info("loading previous checkpoint from %s" % opt.train_from)
        # load the saved the meta-model and override the current one
        model = torch.load(
            open(os.path.join(opt.model_path, opt.exp, '.initial.model'), 'wb')
        )

        if torch.cuda.is_available():
            checkpoint = torch.load(open(opt.train_from, 'rb'))
        else:
            checkpoint = torch.load(
                open(opt.train_from, 'rb'), map_location=lambda storage, loc: storage
            )
        # some compatible problems, keys are started with 'module.'
        checkpoint = dict([(k[7:], v) if k.startswith('module.') else (k, v) for k, v in checkpoint.items()])
        model.load_state_dict(checkpoint)
    else:
        # dump the meta-model
        torch.save(
            model.state_dict(),
            open(os.path.join(opt.train_from[: opt.train_from.find('.epoch=')], 'initial.model'), 'wb')
        )

    if torch.cuda.is_available():
        if len(opt.device_ids) == 1:
            model = model.cuda(device=opt.device_ids[0])
        else:
            model = torch.nn.DataParallel(model, device_ids=opt.device_ids).cuda()

    utils.tally_parameters(model)

    return model


def process_opt(opt):
    if opt.seed > 0:
        torch.manual_seed(opt.seed)

    if torch.cuda.is_available() and not opt.device_ids:
        opt.device_ids = 0

    if hasattr(opt, 'train_ml') and opt.train_ml:
        opt.exp += '.ml'

    if hasattr(opt, 'train_rl') and opt.train_rl:
        opt.exp += '.rl'

    if hasattr(opt, 'copy_attention') and opt.copy_attention:
        opt.exp += '.copy'

    # if hasattr(opt, 'bidirectional') and opt.bidirectional:
    #     opt.exp += '.bi-directional'
    # else:
    #     opt.exp += '.uni-directional'

    # fill time into the name
    if opt.exp_path.find('%s') > 0:
        opt.exp_path = opt.exp_path % (opt.exp, opt.timemark)

    # Path to outputs of predictions.
    setattr(opt, 'pred_path', os.path.join(opt.exp_path, 'pred/'))
    # Path to checkpoints.
    setattr(opt, 'model_path', os.path.join(opt.exp_path, 'model/'))
    # Path to log output.
    setattr(opt, 'log_path', os.path.join(opt.exp_path, 'log/'))
    setattr(opt, 'log_file', os.path.join(opt.log_path, 'output.log'))
    # Path to plots.
    setattr(opt, 'plot_path', os.path.join(opt.exp_path, 'plot/'))

    if not os.path.exists(opt.exp_path):
        os.makedirs(opt.exp_path)
    if not os.path.exists(opt.pred_path):
        os.makedirs(opt.pred_path)
    if not os.path.exists(opt.model_path):
        os.makedirs(opt.model_path)
    if not os.path.exists(opt.log_path):
        os.makedirs(opt.log_path)
    if not os.path.exists(opt.plot_path):
        os.makedirs(opt.plot_path)

    logging.info('EXP_PATH : ' + opt.exp_path)

    # dump the setting (opt) to disk in order to reuse easily
    if opt.train_from:
        opt = torch.load(
            open(os.path.join(opt.model_path, opt.exp + '.initial.config'), 'rb')
        )
    else:
        torch.save(opt,
                   open(os.path.join(opt.model_path, opt.exp + '.initial.config'), 'wb')
                   )
        json.dump(vars(opt), open(os.path.join(opt.model_path, opt.exp + '.initial.json'), 'w'))

    return opt


def main():
    # load settings for training
    parser = argparse.ArgumentParser(
        description='train.py',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    config.preprocess_opts(parser)
    config.model_opts(parser)
    config.train_opts(parser)
    config.predict_opts(parser)
    opt = parser.parse_args()
    opt = process_opt(opt)
    opt.input_feeding = False
    opt.copy_input_feeding = False

    logging = config.init_logging(logger_name=None, log_file=opt.log_file, redirect_to_stdout=False)

    logging.info('Parameters:')
    [logging.info('%s    :    %s' % (k, str(v))) for k, v in opt.__dict__.items()]

    logging.info('======================  Checking GPU Availability  =========================')
    if torch.cuda.is_available():
        if isinstance(opt.device_ids, int):
            opt.device_ids = [opt.device_ids]
        logging.info('Running on %s! devices=%s' % ('MULTIPLE GPUs' if len(opt.device_ids) > 1 else '1 GPU', str(opt.device_ids)))
    else:
        logging.info('Running on CPU!')

    try:
        train_data_loader, valid_data_loader, test_data_loader, word2id, id2word, vocab = load_data_vocab(opt)
        model = init_model(opt)
        optimizer_ml, optimizer_rl, criterion = init_optimizer_criterion(model, opt)
        train_model(model, optimizer_ml, optimizer_rl, criterion, train_data_loader, valid_data_loader, test_data_loader, opt)
    except Exception as e:
        logging.exception("message")


if __name__ == '__main__':
    main()
