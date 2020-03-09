from __future__ import absolute_import, division, print_function

import sys
import os
import argparse
import time
import pdb
import logging

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
try:
    from apex.parallel import DistributedDataParallel as DDP
    from apex.fp16_utils import *
    from apex import amp, optimizers
    from apex.multi_tensor_apply import multi_tensor_applier
    APEX_AVAILABLE = True
except ImportError:
    APEX_AVAILABLE = False

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    pass
import numpy as np
import random
import json
from tqdm import tqdm

from util    import load_config, to_device, to_numpy
from model   import TextGloveCNN, TextGloveDensenetCNN, TextGloveDensenetDSA, TextBertCNN, TextBertCLS
from dataset import prepare_dataset, SnipsGloveDataset, SnipsBertDataset
from early_stopping import EarlyStopping

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def set_seed(opt):
    random.seed(opt.seed)
    np.random.seed(opt.seed)
    torch.manual_seed(opt.seed)
    torch.cuda.manual_seed(opt.seed)

def set_apex_and_distributed(opt):
    if not APEX_AVAILABLE: opt.use_amp = False
    opt.distributed = False
    if 'WORLD_SIZE' in os.environ and opt.use_amp:
        opt.world_size = int(os.environ['WORLD_SIZE'])
        opt.distributed = int(os.environ['WORLD_SIZE']) > 1
    if opt.distributed:
        torch.cuda.set_device(opt.local_rank)
        torch.distributed.init_process_group(backend='nccl', init_method='env://')
        opt.word_size = torch.distributed.get_world_size()

def train_epoch(model, config, train_loader, val_loader, epoch_i):
    device = config['device']
    optimizer = config['optimizer']
    scheduler = config['scheduler']
    writer = config['writer']
    opt = config['opt']

    local_rank = opt.local_rank
    use_amp = opt.use_amp
    criterion = torch.nn.CrossEntropyLoss().to(device)

    # train one epoch
    model.train()
    total_loss = 0.
    final_val_loss = 0.
    total_examples = 0
    st_time = time.time()
    for local_step, (x,y) in tqdm(enumerate(train_loader), total=len(train_loader)):
        global_step = (len(train_loader) * epoch_i) + local_step
        if type(x) != list: # torch.tensor
            x = x.to(device)
        else:               # list of torch.tensor
            for i in range(len(x)):
                x[i] = x[i].to(device)
        y = y.to(device)
        output = model(x)
        loss = criterion(output, y)
        # back-propagation - begin
        optimizer.zero_grad()
        if use_amp:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        optimizer.step()
        # back-propagation - end
        cur_examples = y.size(0)
        total_examples += cur_examples
        total_loss += (loss.item() * cur_examples)
        if local_rank == 0 and writer:
            writer.add_scalar('Loss/train', loss.item(), global_step)
    cur_loss = total_loss / total_examples

    # evaluate
    eval_loss, eval_acc = evaluate(model, config, val_loader, device)
    curr_time = time.time()
    elapsed_time = (curr_time - st_time) / 60
    st_time = curr_time
    curr_lr = scheduler.get_lr()[0] if scheduler else optimizer.param_groups[0]['lr']
    if local_rank == 0:
        logger.info('{:3d} epoch | {:5d}/{:5d} | train loss : {:6.3f}, valid loss {:6.3f}, valid acc {:.4f}| lr :{:7.6f} | {:5.2f} min elapsed'.\
                format(epoch_i, local_step+1, len(train_loader), cur_loss, eval_loss, eval_acc, curr_lr, elapsed_time)) 
        if writer:
            writer.add_scalar('Loss/valid', eval_loss, global_step)
            writer.add_scalar('Acc/valid', eval_acc, global_step)
            writer.add_scalar('LearningRate/train', curr_lr, global_step)
    return eval_loss
 
def evaluate(model, config, val_loader, device):
    model.eval()
    total_loss = 0.
    total_examples = 0 
    correct = 0
    criterion = torch.nn.CrossEntropyLoss().to(device)
    with torch.no_grad():
        for i, (x,y) in enumerate(val_loader):
            if type(x) != list: # torch.tensor
                x = x.to(device)
            else:               # list of torch.tensor
                for i in range(len(x)):
                    x[i] = x[i].to(device)
            y = y.to(device)
            output = model(x)
            loss = criterion(output, y)
            predicted = output.argmax(1)
            correct += (predicted == y).sum().item()
            cur_examples = y.size(0)
            total_loss += (loss.item() * cur_examples) 
            total_examples += cur_examples
    cur_loss = total_loss / total_examples
    cur_acc  = correct / total_examples
    return cur_loss, cur_acc

def save_model(model, opt, config):
    checkpoint_path = opt.save_path
    with open(checkpoint_path, 'wb') as f:
        if opt.use_amp:
            checkpoint = {
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'amp': amp.state_dict()
                    }
        else:
            checkpoint = model.state_dict()
        torch.save(checkpoint,f)

def train(opt):
    device = torch.device(opt.device)

    # set seed, distributed setting, etc
    set_seed(opt)
    set_apex_and_distributed(opt)
    torch.autograd.set_detect_anomaly(True)

    # set config
    config = load_config(opt)
    config['device'] = device
    config['opt'] = opt
    logger.info("%s", config)
  
    # prepare train, valid dataset
    if config['emb_class'] == 'glove':
        filepath = os.path.join(opt.data_dir, 'train.txt.ids')
        train_loader = prepare_dataset(opt, filepath, SnipsGloveDataset, shuffle=True, num_workers=2)
        filepath = os.path.join(opt.data_dir, 'valid.txt.ids')
        valid_loader = prepare_dataset(opt, filepath, SnipsGloveDataset, shuffle=False, num_workers=2)
    if 'bert' in config['emb_class']:
        filepath = os.path.join(opt.data_dir, 'train.txt.fs')
        train_loader = prepare_dataset(opt, filepath, SnipsBertDataset, shuffle=True, num_workers=2)
        filepath = os.path.join(opt.data_dir, 'valid.txt.fs')
        valid_loader = prepare_dataset(opt, filepath, SnipsBertDataset, shuffle=False, num_workers=2)

    label_path = os.path.join(opt.data_dir, opt.label_filename)
    # prepare model
    if config['emb_class'] == 'glove':
        if config['enc_class'] == 'cnn':
            # set embedding as trainable
            embedding_path = os.path.join(opt.data_dir, opt.embedding_filename)
            emb_non_trainable = not opt.embedding_trainable
            model = TextGloveCNN(config, embedding_path, label_path, emb_non_trainable=emb_non_trainable)
        if config['enc_class'] == 'densenet-cnn':
            # set embedding as trainable
            embedding_path = os.path.join(opt.data_dir, opt.embedding_filename)
            emb_non_trainable = not opt.embedding_trainable
            model = TextGloveDensenetCNN(config, embedding_path, label_path, emb_non_trainable=emb_non_trainable)
        if config['enc_class'] == 'densenet-dsa':
            # set embedding as trainable
            embedding_path = os.path.join(opt.data_dir, opt.embedding_filename)
            emb_non_trainable = not opt.embedding_trainable
            model = TextGloveDensenetDSA(config, embedding_path, label_path, emb_non_trainable=emb_non_trainable)
    if 'bert' in config['emb_class']:
        from transformers import BertTokenizer, BertConfig, BertModel
        from transformers import AlbertTokenizer, AlbertConfig, AlbertModel
        MODEL_CLASSES = {
            "bert": (BertConfig, BertTokenizer, BertModel),
            "albert": (AlbertConfig, AlbertTokenizer, AlbertModel)
        }
        Config    = MODEL_CLASSES[config['emb_class']][0]
        Tokenizer = MODEL_CLASSES[config['emb_class']][1]
        Model     = MODEL_CLASSES[config['emb_class']][2]
        bert_tokenizer = Tokenizer.from_pretrained(opt.bert_model_name_or_path,
                                                   do_lower_case=opt.bert_do_lower_case)
        bert_model = Model.from_pretrained(opt.bert_model_name_or_path,
                                           from_tf=bool(".ckpt" in opt.bert_model_name_or_path))
        bert_config = bert_model.config
        ModelClass = TextBertCNN
        if config['enc_class'] == 'cls': ModelClass = TextBertCLS
        model = ModelClass(config, bert_config, bert_model, label_path, feature_based=opt.bert_use_feature_based)
    model.to(device)
    print(model)
    logger.info("[Model prepared]")

    # create optimizer, scheduler, summary writer
    logger.info("[Creating optimizer, scheduler, summary writer...]")
    opt.one_epoch_step = (len(train_loader) // (opt.batch_size*opt.world_size))
    optimizer = torch.optim.Adam(model.parameters(), lr=opt.lr, weight_decay=opt.l2norm)
    if opt.use_amp:
        model, optimizer = amp.initialize(model, optimizer, opt_level=opt.opt_level)
    if opt.distributed:
        model = DDP(model, delay_allreduce=True)
    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer=optimizer, gamma=opt.decay_rate)
    try:
        writer = SummaryWriter(log_dir=opt.log_dir)
    except:
        writer = None
    logger.info("[Ready]")

    # training

    # additional config setting for parameter passing
    config['optimizer'] = optimizer
    config['scheduler'] = scheduler
    config['writer'] = writer

    early_stopping = EarlyStopping(logger, patience=opt.patience, measure='loss', verbose=1)
    local_worse_steps = 0
    prev_eval_loss = float('inf')
    best_eval_loss = float('inf')
    for epoch_i in range(opt.epoch):
        epoch_st_time = time.time()
        eval_loss = train_epoch(model, config, train_loader, valid_loader, epoch_i)
        # early stopping
        if early_stopping.validate(eval_loss, measure='loss'): break
        if opt.local_rank == 0 and eval_loss < best_eval_loss:
            best_eval_loss = eval_loss
            if opt.save_path:
                save_model(model, opt, config)
                if 'bert' in config['emb_class']:
                    if not os.path.exists(opt.bert_output_dir):
                        os.makedirs(opt.bert_output_dir)
                    bert_tokenizer.save_pretrained(opt.bert_output_dir)
                    bert_model.save_pretrained(opt.bert_output_dir)
            early_stopping.reset(best_eval_loss)
        early_stopping.status()
        # begin: scheduling, apply rate decay at the measure(ex, loss) getting worse for the number of deacy epoch.
        if prev_eval_loss <= eval_loss:
            local_worse_steps += 1
        else:
            local_worse_steps = 0
        logger.info('Scheduler: local_worse_steps / opt.decay_steps = %d / %d' % (local_worse_steps, opt.decay_steps))
        if epoch_i > opt.warmup_steps and (local_worse_steps >= opt.decay_steps or early_stopping.step() > opt.decay_steps):
            scheduler.step()
        prev_eval_loss = eval_loss
        # end: scheduling

def main():
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--data_dir', type=str, default='data/snips')
    parser.add_argument('--embedding_filename', type=str, default='embedding.npy')
    parser.add_argument('--label_filename', type=str, default='label.txt')
    parser.add_argument('--config', type=str, default='configs/config-glove-cnn.json')
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--use_amp', action="store_true")
    parser.add_argument('--batch_size', type=int, default=128)
    parser.add_argument('--epoch', type=int, default=64)
    parser.add_argument('--lr', type=float, default=2e-4)
    parser.add_argument('--decay_rate', type=float, default=1.0)
    parser.add_argument('--decay_steps', type=float, default=2)
    parser.add_argument('--warmup_steps', type=int, default=4)
    parser.add_argument('--patience', default=7, type=int)
    parser.add_argument('--save_path', type=str, default='pytorch-model.pt')
    parser.add_argument('--l2norm', type=float, default=1e-6)
    parser.add_argument('--tmax',type=int, default=-1)
    parser.add_argument('--opt-level', type=str, default='O1')
    parser.add_argument('--local_rank', default=0, type=int)
    parser.add_argument('--world_size', default=1, type=int)
    parser.add_argument('--log_dir', type=str, default='runs')
    parser.add_argument('--seed', default=5, type=int)
    parser.add_argument('--embedding_trainable', action='store_true')
    # for BERT
    parser.add_argument('--bert_model_name_or_path', type=str, default='embeddings/bert-base-uncased',
                        help="Path to pre-trained model or shortcut name(ex, bert-base-uncased)")
    parser.add_argument('--bert_do_lower_case', action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument('--bert_output_dir', type=str, default='bert-checkpoint',
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument('--bert_use_feature_based', action='store_true',
                        help="use BERT as feature-based, default fine-tuning")

    opt = parser.parse_args()

    train(opt)
   
if __name__ == '__main__':
    main()
