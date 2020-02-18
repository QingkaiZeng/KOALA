'''
 @Date  : 12/11/2019
 @Author: Zhihan Zhang
 @mail  : zhangzhihan@pku.edu.cn
 @homepage: ytyz1307zzh.github.io
'''
import time
print('[INFO] Starting import...')
import_start_time = time.time()
import torch
import json
import os
import pdb
import numpy as np
from typing import List, Dict
from Constants import *
import argparse
# from torchsummaryX import summary
# from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader
from allennlp.modules.elmo import batch_to_ids
from utils import *
from predict import *
from Dataset import *
from Model import *
import datetime as dt
print(f'[INFO] Import modules time: {time.time() - import_start_time}s')
torch.set_printoptions(precision=3, edgeitems=6, sci_mode=False)


parser = argparse.ArgumentParser()

# model parameters
parser.add_argument('-per_gpu_batch_size', type=int, default=64)
parser.add_argument('-embed_size', type=int, default=128, help="embedding size (including the verb indicator)")
parser.add_argument('-hidden_size', type=int, default=128, help="hidden size of lstm")
parser.add_argument('-lr', type=float, default=1e-3, help="learning rate")
parser.add_argument('-dropout', type=float, default=0.5, help="dropout rate")
parser.add_argument('-elmo_dropout', type=float, default=0.5, help="dropout rate of elmo embedding")
parser.add_argument('-loc_loss', type=float, default=0.3, help="hyper-parameter to weight location loss and state_loss")
parser.add_argument('-max_grad_norm', default=1.0, type=float, help="Max gradient norm")
parser.add_argument('-grad_accum_step', default=1, type=int, help='gradient accumulation steps')

# training parameters
parser.add_argument('-mode', type=str, choices=['train', 'test'], default='train', help="train or test")
parser.add_argument('-ckpt_dir', type=str, default=None, help="checkpoint directory")
parser.add_argument('-save_mode', type=str, choices=['best', 'all', 'none', 'last'], default='best',
                    help="best (default): save checkpoints when reaching new best score; all: save all checkpoints; none: don't save")
parser.add_argument('-epoch', type=int, default=100, help="number of epochs, use -1 to rely on early stopping only")
parser.add_argument('-impatience', type=int, default=20, help='number of evaluation rounds for early stopping, use -1 to disable early stopping')
parser.add_argument('-report', type=int, default=2, help="report frequence per epoch, should be at least 1")
parser.add_argument('-elmo_dir', type=str, default='elmo', help="directory that contains options and weight files for allennlp Elmo")
parser.add_argument('-train_set', type=str, default="data/train.json", help="path to training set")
parser.add_argument('-dev_set', type=str, default="data/dev.json", help="path to dev set")

# test parameters
parser.add_argument('-test_set', type=str, default="data/test.json", help="path to test set")
parser.add_argument('-restore', type=str, default=None, help="restoring model path")
parser.add_argument('-dummy_test', type=str, default="data/dummy-predictions.tsv", help="path to dummy prediction file")
parser.add_argument('-output', type=str, default=None, help="path to store prediction outputs")

# commonsense parameters
parser.add_argument('-cpnet', type=str, default="ConceptNet/result/retrieval.json", help="path to conceptnet triples")
parser.add_argument('-wiki', type=str, default="wiki/result/retrieval.json", help="path to wiki paragraphs")
parser.add_argument('-cpnet_enc_class', type=str, default='roberta', help='conceptnet encoder class')
parser.add_argument('-cpnet_enc_name', type=str, default='roberta-large', help='conceptnet encoder name')
parser.add_argument('-cpnet_finetune', action='store_true', default=False, help='specify to fine-tune the language model')
parser.add_argument('-cpnet_inject', choices=['state', 'location', 'both'], default='both',
                    help='where to inject ConceptNet commonsense')

# other parameters
parser.add_argument('-debug', action='store_true', default=False, help="enable debug mode, change data files to debug data")
parser.add_argument('-no_cuda', action='store_true', default=False, help="if true, will only use cpu")

opt = parser.parse_args()

try:
    opt.n_gpu = len(os.environ["CUDA_VISIBLE_DEVICES"].split(','))
except KeyError:  # did not specify device from cmd
    opt.n_gpu = 1
opt.batch_size = opt.per_gpu_batch_size * opt.n_gpu

if opt.ckpt_dir and not os.path.exists(opt.ckpt_dir):
    os.mkdir(opt.ckpt_dir)
if opt.ckpt_dir:
    log_file = open(os.path.join(opt.ckpt_dir, 'train.log'), 'w', encoding='utf-8')


def output(text):
    print(text)
    if opt.ckpt_dir:
        print(text, file = log_file)


output('Received arguments:')
output(opt)
output('-' * 50)

assert opt.report >= 1

torch.manual_seed(1234)
if opt.n_gpu > 0:
    torch.cuda.manual_seed_all(1234)


def save_model(path: str, model: nn.Module):
    if opt.save_mode == 'none':
        return

    if not opt.ckpt_dir:
        print("[ERROR] Intended to store checkpoint but no checkpoint directory is specified.")
        raise RuntimeError("Did not specify -ckpt_dir option")

    model_to_save = model.module if hasattr(model, "module") else model

    model_state_dict = model_to_save.state_dict()
    torch.save(model_state_dict, path)


def train():

    train_set = ProparaDataset(opt.train_set, cpnet_path=opt.cpnet, is_test = False)
    shuffle_train = True
    if opt.debug:
        print('*'*20 + '[INFO] Debug mode enabled. Switch training set to debug.json' + '*'*20)
        train_set = ProparaDataset('data/debug.json', cpnet_path=opt.cpnet, is_test = False)
        shuffle_train = False

    train_batch = DataLoader(dataset = train_set, batch_size = opt.batch_size, shuffle = shuffle_train, collate_fn = Collate())
    dev_set = ProparaDataset(opt.dev_set, cpnet_path=opt.cpnet, is_test = False)

    if opt.debug:
        print('*'*20 + '[INFO] Debug mode enabled. Switch dev set to debug.json' + '*'*20)
        dev_set = ProparaDataset('data/debug.json', cpnet_path=opt.cpnet, is_test = False)

    model = NCETModel(opt = opt, is_test = False)
    if not opt.no_cuda:
        model.cuda()
        if opt.n_gpu > 1:
            model = nn.DataParallel(model)

    optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=opt.lr)
    best_score = np.NINF
    impatience = 0
    epoch_i = 0

    if opt.epoch == -1:
        opt.epoch = np.inf

    if opt.impatience == -1:
        opt.impatience = np.inf

    print('Start training...')
    model.zero_grad()

    while epoch_i < opt.epoch:

        model.train()
        train_instances = len(train_set)

        start_time = time.time()
        report_state_loss, report_loc_loss = 0, 0
        report_state_correct, report_state_pred = 0, 0
        report_loc_correct, report_loc_pred = 0, 0
        batch_cnt = 0

        if train_instances % opt.batch_size == 0:
            total_batches = train_instances // opt.batch_size
        else:
            total_batches = train_instances // opt.batch_size + 1
        report_batch = get_report_time(total_batches = total_batches,
                                       report_times = opt.report,
                                       grad_accum_step = opt.grad_accum_step)  # when to report results

        for batch in train_batch:
            # with open('logs/debug.log', 'w', encoding='utf-8') as debug_file:
            #     torch.set_printoptions(threshold=np.inf)
            #     print(batch, file = debug_file)

            paragraphs = batch['paragraph']
            char_paragraph = batch_to_ids(paragraphs)
            sentence_mask = batch['sentence_mask']
            entity_mask = batch['entity_mask']
            verb_mask = batch['verb_mask']
            loc_mask = batch['loc_mask']
            gold_loc_seq = batch['gold_loc_seq']
            gold_state_seq = batch['gold_state_seq']
            cpnet_triples = batch['cpnet']
            metadata = batch['metadata']
            num_cands = torch.IntTensor([meta['total_loc_cands'] for meta in metadata])

            if not opt.no_cuda:
                char_paragraph = char_paragraph.cuda()
                sentence_mask = sentence_mask.cuda()
                entity_mask = entity_mask.cuda()
                verb_mask = verb_mask.cuda()
                loc_mask = loc_mask.cuda()
                gold_loc_seq = gold_loc_seq.cuda()
                gold_state_seq = gold_state_seq.cuda()
                num_cands = num_cands.cuda()

            if batch_cnt == 0 or batch_cnt in report_batch:
                print_hidden = True
            else:
                print_hidden = False

            train_result = model(char_paragraph = char_paragraph, entity_mask = entity_mask, verb_mask = verb_mask,
                                 loc_mask = loc_mask, gold_loc_seq = gold_loc_seq, gold_state_seq = gold_state_seq,
                                 num_cands = num_cands, sentence_mask = sentence_mask, cpnet_triples = cpnet_triples,
                                 print_hidden = print_hidden)

            train_state_loss, train_loc_loss, train_state_correct, train_state_pred,\
                train_loc_correct, train_loc_pred = train_result

            if opt.n_gpu > 1:
                train_state_loss = train_state_loss.mean()
                train_loc_loss = train_loc_loss.mean()

            train_loss = train_state_loss + opt.loc_loss * train_loc_loss
            if opt.grad_accum_step > 1:
                train_loss = train_loss / opt.grad_accum_step
            train_loss.backward()

            report_state_loss += train_state_loss.item() * train_state_pred
            report_loc_loss += train_loc_loss.item() * train_loc_pred
            report_state_correct += train_state_correct
            report_state_pred += train_state_pred
            report_loc_correct += train_loc_correct
            report_loc_pred += train_loc_pred
            batch_cnt += 1

            if batch_cnt % opt.grad_accum_step == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), opt.max_grad_norm)
                optimizer.step()
                model.zero_grad()

                # time to report results
                if batch_cnt in report_batch:

                    state_loss = report_state_loss / report_state_pred  # average over all elements
                    loc_loss = report_loc_loss / report_loc_pred
                    total_loss = state_loss + opt.loc_loss * loc_loss
                    state_accuracy = report_state_correct / report_state_pred
                    loc_accuracy = report_loc_correct / report_loc_pred
                    total_accuracy = (report_state_correct + report_loc_correct) / (report_state_pred + report_loc_pred)

                    output('*' * 50)
                    output(f'{batch_cnt}/{total_batches}, Epoch {epoch_i+1}:\n'
                           f'Loss: {total_loss:.3f}, State Loss: {state_loss:.3f}, '
                           f'Location Loss: {loc_loss:.3f}\n'
                           f'Total Accuracy: {total_accuracy*100:.3f}%, '
                           f'State Prediction Accuracy: {state_accuracy*100:.3f}%, '
                           f'Location Accuracy: {loc_accuracy*100:.3f}% \n'
                           f'Time Elapse: {time.time()-start_time:.2f}s')
                    output('-' * 50)

                    model.eval()
                    eval_score = evaluate(dev_set, model)
                    model.train()

                    if eval_score > best_score:  # new best score
                        best_score = eval_score
                        impatience = 0
                        output('New best score!')
                        if opt.save_mode == 'all':
                            save_model(os.path.join(opt.ckpt_dir, f'best_checkpoint_{best_score:.3f}.pt'), model)
                        elif opt.save_mode == 'best':
                            save_model(os.path.join(opt.ckpt_dir, f'best_checkpoint.pt'), model)
                    else:
                        impatience += 1
                        output(f'Impatience: {impatience}, best score: {best_score:.3f}.')
                        if opt.save_mode == 'all':
                            save_model(os.path.join(opt.ckpt_dir, f'checkpoint_{eval_score:.3f}.pt'), model)
                        if impatience >= opt.impatience:
                            output('Early Stopping!')
                            if opt.save_mode == 'last':
                                save_model(os.path.join(opt.ckpt_dir, f'checkpoint_{eval_score:.3f}.pt'), model)
                            quit()

                    report_state_loss, report_loc_loss = 0, 0
                    report_state_correct, report_state_pred = 0, 0
                    report_loc_correct, report_loc_pred = 0, 0
                    start_time = time.time()

        epoch_i += 1

    if opt.save_mode == 'last':
        save_model(os.path.join(opt.ckpt_dir, f'checkpoint_{eval_score:.3f}.pt'), model)


        # summary(model, char_paragraph, entity_mask, verb_mask, loc_mask)
        # with SummaryWriter() as writer:
        #     writer.add_graph(model, (char_paragraph, entity_mask, verb_mask, loc_mask, gold_loc_mask, gold_state_mask))


def evaluate(dev_set, model):
    dev_batch = DataLoader(dataset = dev_set, batch_size = opt.batch_size, shuffle = False, collate_fn = Collate())

    start_time = time.time()
    report_state_loss, report_loc_loss = 0, 0
    report_state_correct, report_state_pred = 0, 0
    report_loc_correct, report_loc_pred = 0, 0
    batch_cnt = 0

    with torch.no_grad():
        for batch in dev_batch:

            paragraphs = batch['paragraph']
            char_paragraph = batch_to_ids(paragraphs)
            sentence_mask = batch['sentence_mask']
            entity_mask = batch['entity_mask']
            verb_mask = batch['verb_mask']
            loc_mask = batch['loc_mask']
            gold_loc_seq = batch['gold_loc_seq']
            gold_state_seq = batch['gold_state_seq']
            cpnet_triples = batch['cpnet']
            metadata = batch['metadata']
            num_cands = torch.IntTensor([meta['total_loc_cands'] for meta in metadata])

            if not opt.no_cuda:
                char_paragraph = char_paragraph.cuda()
                sentence_mask = sentence_mask.cuda()
                entity_mask = entity_mask.cuda()
                verb_mask = verb_mask.cuda()
                loc_mask = loc_mask.cuda()
                gold_loc_seq = gold_loc_seq.cuda()
                gold_state_seq = gold_state_seq.cuda()
                num_cands = num_cands.cuda()

            if batch_cnt == 0:
                print_hidden = True
            else:
                print_hidden = False

            eval_result = model(char_paragraph = char_paragraph, entity_mask = entity_mask, verb_mask = verb_mask,
                                loc_mask = loc_mask, gold_loc_seq = gold_loc_seq, gold_state_seq = gold_state_seq,
                                num_cands = num_cands, sentence_mask = sentence_mask, cpnet_triples = cpnet_triples,
                                print_hidden = print_hidden)

            eval_state_loss, eval_loc_loss, eval_state_correct, eval_state_pred, \
                eval_loc_correct, eval_loc_pred = eval_result

            if opt.n_gpu > 1:
                eval_state_loss = eval_state_loss.mean()
                eval_loc_loss = eval_loc_loss.mean()

            report_state_loss += eval_state_loss.item() * eval_state_pred
            report_loc_loss += eval_loc_loss.item() * eval_loc_pred
            report_state_correct += eval_state_correct
            report_state_pred += eval_state_pred
            report_loc_correct += eval_loc_correct
            report_loc_pred += eval_loc_pred

            batch_cnt += 1

    state_loss = report_state_loss / report_state_pred  # average over all elements
    loc_loss = report_loc_loss / report_loc_pred
    total_loss = state_loss + opt.loc_loss * loc_loss
    total_accuracy = (report_state_correct + report_loc_correct) / (report_state_pred + report_loc_pred)
    state_accuracy = report_state_correct / report_state_pred
    loc_accuracy = report_loc_correct / report_loc_pred

    output(f'\tEvaluation:\n'
           f'\tLoss: {total_loss:.3f}, State Loss: {state_loss:.3f}, '
           f'Location Loss: {loc_loss:.3f}\n'
           f'\tTotal Accuracy: {total_accuracy * 100:.3f}%, '
           f'State Prediction Accuracy: {state_accuracy * 100:.3f}%, '
           f'Location Accuracy: {loc_accuracy * 100:.3f}% \n'
           f'\tTime Elapse: {time.time() - start_time:.2f}s')
    output('*' * 50)

    return total_accuracy * 100


def test(test_set, model):

    print('[INFO] Start testing...')
    test_batch = DataLoader(dataset = test_set, batch_size = opt.batch_size, shuffle = False, collate_fn = Collate())

    start_time = time.time()
    report_state_correct, report_state_pred = 0, 0
    report_loc_correct, report_loc_pred = 0, 0
    output_result = {}
    batch_cnt = 0

    with torch.no_grad():
        for batch in test_batch:

            paragraphs = batch['paragraph']
            char_paragraph = batch_to_ids(paragraphs)
            sentence_mask = batch['sentence_mask']
            entity_mask = batch['entity_mask']
            verb_mask = batch['verb_mask']
            loc_mask = batch['loc_mask']
            gold_loc_seq = batch['gold_loc_seq']
            gold_state_seq = batch['gold_state_seq']
            cpnet_triples = batch['cpnet']
            metadata = batch['metadata']
            num_cands = torch.IntTensor([meta['total_loc_cands'] for meta in metadata])

            if not opt.no_cuda:
                char_paragraph = char_paragraph.cuda()
                sentence_mask = sentence_mask.cuda()
                entity_mask = entity_mask.cuda()
                verb_mask = verb_mask.cuda()
                loc_mask = loc_mask.cuda()
                gold_loc_seq = gold_loc_seq.cuda()
                gold_state_seq = gold_state_seq.cuda()
                num_cands = num_cands.cuda()

            if batch_cnt == 0:
                print_hidden = True
            else:
                print_hidden = False

            test_result = model(char_paragraph=char_paragraph, entity_mask=entity_mask, verb_mask=verb_mask,
                                loc_mask=loc_mask, gold_loc_seq=gold_loc_seq, gold_state_seq=gold_state_seq,
                                num_cands=num_cands, sentence_mask=sentence_mask, cpnet_triples=cpnet_triples,
                                print_hidden=print_hidden)

            pred_state_seq, pred_loc_seq, test_state_correct, test_state_pred,\
                test_loc_correct, test_loc_pred = test_result

            batch_size = len(paragraphs)
            for i in range(batch_size):
                pred_instance = get_output(metadata = metadata[i], pred_state_seq = pred_state_seq[i], pred_loc_seq = pred_loc_seq[i])
                para_id = pred_instance['id']
                entity_name = pred_instance['entity']
                output_result[str(para_id) + '-' + entity_name] = pred_instance

            report_state_correct += test_state_correct
            report_state_pred += test_state_pred
            report_loc_correct += test_loc_correct
            report_loc_pred += test_loc_pred

            batch_cnt += 1

    total_accuracy = (report_state_correct + report_loc_correct) / (report_state_pred + report_loc_pred)
    state_accuracy = report_state_correct / report_state_pred
    loc_accuracy = report_loc_correct / report_loc_pred

    output(f'Test:\n'
           f'Total Accuracy: {total_accuracy * 100:.3f}%, '
           f'State Prediction Accuracy: {state_accuracy * 100:.3f}%, '
           f'Location Accuracy: {loc_accuracy * 100:.3f}%')

    write_output(output = output_result, dummy_filepath = opt.dummy_test, output_filepath = opt.output)
    print(f'[INFO] Test finished. Time elapse: {time.time() - start_time}s')


if __name__ == "__main__":

    if opt.mode == 'train':
        train()

    elif opt.mode == 'test':
        if not opt.restore:
            print("[ERROR] Entered test mode but no restore file is specified.")
            raise RuntimeError("Did not specify -restore option")

        if not opt.output:
            print("[ERROR] Entered test mode but no output file is specified.")
            raise RuntimeError("Did not specify -output option")

        if not opt.output.endswith('.tsv'):
            print("[WARNING] The output will be in TSV format, while the specified output file does not have .tsv suffix.")

        if opt.debug:
            print('*' * 20 + '[INFO] Debug mode enabled. Switch dummy file to data/dummy-debug.json' + '*' * 20)
            opt.dummy_test = 'data/dummy-debug.tsv'

        test_set = ProparaDataset(opt.test_set, cpnet_path=opt.cpnet, is_test=True)

        if opt.debug:
            print('*' * 20 + '[INFO] Debug mode enabled. Switch test set to debug.json' + '*' * 20)
            test_set = ProparaDataset('data/debug.json', cpnet_path=opt.cpnet, is_test=True)

        print('[INFO] Start loading trained model...')
        restore_start_time = time.time()
        model = NCETModel(opt = opt, is_test = True)
        model_state_dict = torch.load(opt.restore)
        model.load_state_dict(model_state_dict)
        model.eval()
        print(f'[INFO] Loaded model from {opt.restore}, time elapse: {time.time() - restore_start_time}s')

        if not opt.no_cuda:
            model.cuda()

        test(test_set, model)