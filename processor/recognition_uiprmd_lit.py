#!/usr/bin/env python
# pylint: disable=W0201
import os
import sys
import argparse
import yaml
import numpy as np

# torch
import torch
import torch.nn as nn
import torch.optim as optim

# torchlight
import torchlight
from torchlight import str2bool
from torchlight import DictAction
from torchlight import import_class

from .processor_uiprmd_lit import Processor

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv1d') != -1:
        m.weight.data.normal_(0.0, 0.02)
        if m.bias is not None:
            m.bias.data.fill_(0)
    elif classname.find('Conv2d') != -1:
        m.weight.data.normal_(0.0, 0.02)
        if m.bias is not None:
            m.bias.data.fill_(0)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)

class REC_Processor(Processor):
    """
        Processor for Skeleton-based Action Recgnition
    """

    def load_model(self):
        self.model = self.io.load_model(self.arg.model,
                                        **(self.arg.model_args))
        self.model.apply(weights_init)
        self.loss = nn.CrossEntropyLoss()
        #self.cosine_simi = nn.CosineSimilarity(dim=1, eps=1e-6)
        self.loss_att = nn.BCELoss()
    def load_optimizer(self):
        if self.arg.optimizer == 'SGD':
            self.optimizer = optim.SGD(
                self.model.parameters(),
                lr=self.arg.base_lr,
                momentum=0.9,
                nesterov=self.arg.nesterov,
                weight_decay=self.arg.weight_decay)
        elif self.arg.optimizer == 'Adam':
            self.optimizer = optim.Adam(
                self.model.parameters(),
                lr=self.arg.base_lr,
                weight_decay=self.arg.weight_decay)
        else:
            raise ValueError()

    def adjust_lr(self):
        if self.arg.optimizer == 'SGD' and self.arg.step:
            lr = self.arg.base_lr * (
                0.1**np.sum(self.meta_info['epoch']>= np.array(self.arg.step)))
            for param_group in self.optimizer.param_groups:
                param_group['lr'] = lr
            self.lr = lr
        else:
            self.lr = self.arg.base_lr

    def show_topk(self, k):
        rank = self.result.argsort()
        hit_top_k = [l in rank[i, -k:] for i, l in enumerate(self.label)]
        accuracy = sum(hit_top_k) * 1.0 / len(hit_top_k)
        self.io.print_log('\tTop{}: {:.2f}%'.format(k, 100 * accuracy))
        # Bruce 20190918
        #self.epoch_info['top {}'.format(k)] = '{:.2f}'.format(100 * accuracy)
        if k==1:
            self.progress_info[int(self.meta_info['epoch']/self.arg.eval_interval), 2]  =  100 * accuracy
            if accuracy >= self.meta_info['best_t1']:
                self.meta_info['best_t1'] = accuracy
                self.meta_info['is_best'] = True
                self.save_recall_precision(self.meta_info['epoch'])
        else:
            self.progress_info[int(self.meta_info['epoch']/self.arg.eval_interval), 3]  =  100 * accuracy

    def save_recall_precision(self, epoch): #original input: (label, score),score refers to self.result
        instance_num, class_num = self.result.shape
        rank = self.result.argsort()
        confusion_matrix = np.zeros([class_num, class_num])

        for i in range(instance_num):
            true_l = self.label[i]
            pred_l = rank[i, -1]
            confusion_matrix[true_l][pred_l] += 1
        #np.savetxt("confusion_matrix.csv", confusion_matrix, fmt='%.3e', delimiter=",")
        np.savetxt(os.path.join(self.arg.work_dir,'confusion_matrix_epoch_{}.csv').format(epoch+1), confusion_matrix, fmt='%d', delimiter=",")

        precision = []
        recall = []

        for i in range(class_num):
            true_p = confusion_matrix[i][i]
            false_n = sum(confusion_matrix[i, :]) - true_p
            false_p = sum(confusion_matrix[:, i]) - true_p
            precision_ = true_p * 1.0 / (true_p + false_p)
            recall_ = true_p * 1.0 / (true_p + false_n)
            if np.isnan(precision_):
                precision_ = 0
            if np.isnan(recall_):
                recall_ = 0
            precision.append(precision_)
            recall.append(recall_)
        recall = np.asarray(recall)
        precision = np.asarray(precision)
        labels = np.asarray(range(1,class_num+1))
        res = np.column_stack([labels.T, recall.T, precision.T])
        np.savetxt(os.path.join(self.arg.work_dir,'recall_precision_epoch_{}.csv'.format(epoch+1)), res, fmt='%f', delimiter=",", header="Label,  Recall,  Precision")

    def train(self):

        self.model.train()
        self.adjust_lr()
        loader = self.data_loader['train']
        #loss_value = []
        loss_value_cls = []
        loss_cos = []
        #loss_value_skl = []
        #for data, rgb, label, atten in loader:
        for data_pos, data_ang, label in loader:
            # get data
            #with torch.cuda.device(0):
            data_pos = data_pos.float().cuda(async=True)
            data_ang = data_ang.float().cuda(async=True)
            label = label.long().cuda(async=True)
            data_pos = torch.autograd.Variable(data_pos, volatile=False)
            data_ang = torch.autograd.Variable(data_ang, volatile=False)
            label = torch.autograd.Variable(label, volatile=False)
            #print('data:\n',data)
            #print('label:\n',label)
            #data = data.float().to(self.dev)
            #label = label.long().to(self.dev)
            #rgb = rgb.float().to(self.dev)
            #atten = atten.float().to(self.dev)

            # forward
            #output, att_roi, att_skl = self.model(data, rgb)
            output, cos = self.model(data_pos, data_ang)
            #output = self.model(data_pos, data_ang)
            loss_cls = self.loss(output, label)

            # RGB modal atten loss
            #att_roi_loss = self.loss_att(att_roi, atten)
            # mutual atten loss on skeleton from RGB
            #att_skl_loss = self.loss_att(att_skl, 1 - atten)
            #print('?????????',loss_cls.size(),cos.size())
            #print(loss_cls, torch.sum(cos))
            cos = torch.sum(cos)
            loss = loss_cls + 0.1 * cos

            # backward
            self.optimizer.zero_grad()
            #loss.backward()
            loss.backward()
            self.optimizer.step()

            # statistics
            #self.iter_info['loss'] = loss.data.item()
            self.iter_info['ls_cls'] = loss_cls.data.item()
            self.iter_info['cos'] = cos.data.item()
            #self.iter_info['ls_at_skl'] = att_skl_loss.data.item()
            self.iter_info['lr'] = '{:.6f}'.format(self.lr)
            #loss_value.append(self.iter_info['loss'])
            loss_value_cls.append(self.iter_info['ls_cls'])
            loss_cos.append(self.iter_info['cos'])
            #loss_value_skl.append(self.iter_info['ls_at_skl'])
            self.show_iter_info()
            self.meta_info['iter'] += 1

        #self.epoch_info['mean loss']= np.mean(loss_value)
        self.epoch_info['ls_cls']= np.mean(loss_value_cls)
        self.epoch_info['cos']= np.mean(loss_cos)
        #self.epoch_info['ls_at_skl']= np.mean(loss_value_skl)
        self.show_epoch_info()

        self.io.print_timer()

    def test(self, evaluation=True):

        self.model.eval()
        loader = self.data_loader['test']
        #loss_value = []
        result_frag = []
        label_frag = []

        loss_value_cls = []
        loss_cos = []
        #loss_value_skl = []

        #for data, rgb, label, atten in loader:
        for data_pos, data_ang, label in loader:
            # get data
            data_pos = data_pos.float().to(self.dev)
            data_ang = data_ang.float().to(self.dev)
            label = label.long().to(self.dev)
            #rgb = rgb.float().to(self.dev)
            #atten = atten.float().to(self.dev)
            # inference
            with torch.no_grad():
                #output, att_roi, att_skl = self.model(data, rgb)
                output, cos = self.model(data_pos,data_ang)
                #output = self.model(data_pos,data_ang)

            result_frag.append(output.data.cpu().numpy())

            # get loss
            if evaluation:
                loss_cls = self.loss(output, label)
                # mutual atten loss on skeleton from RGB
                #att_roi_loss = self.loss_att(att_roi, atten)
                #att_skl_loss = self.loss_att(att_skl, 1 - atten)
                #loss = loss_cls + att_roi_loss + att_skl_loss
                #loss_value.append(loss.data.item())
                loss_value_cls.append(loss_cls.data.item())
                cos = torch.sum(cos)
                loss_cos.append(cos.data.item())
                #loss_value_skl.append(att_skl_loss.data.item())

                label_frag.append(label.data.cpu().numpy())

        self.result = np.concatenate(result_frag)
        #np.savetxt('../../data/UI_PRMD/otpt_st_gcn/c_inc/output_single_1.csv', self.result, fmt='%f', delimiter=",", header="A1, A2, A3, A4, A5, A6, A7, A8, A9, A10")
        #np.savetxt('../../data/UI_PRMD/otpt_st_gcn/c_inc/kinect/ang/ang_07.csv', self.result, fmt='%f', delimiter=",", header="A1, A2")
        #np.savetxt(self.arg.work_dir+'/result.csv', self.result, fmt='%f', delimiter=",", header="A1, A2")

        #print(self.result.size())
        #np.save('test3.npy', label_frag)
        if evaluation:
            self.label = np.concatenate(label_frag)
            #self.epoch_info['mean loss']= np.mean(loss_value)
            self.epoch_info['ls_cls']= np.mean(loss_value_cls)
            self.epoch_info['cos']= np.mean(loss_cos)
            #self.epoch_info['ls_at_skl']= np.mean(loss_value_skl)
            self.show_epoch_info()

            # show top-k accuracy
            for k in self.arg.show_topk:
                self.show_topk(k)

            #self.save_recall_precision(self.meta_info['epoch'])

    @staticmethod
    def get_parser(add_help=False):

        # parameter priority: command line > config > default
        parent_parser = Processor.get_parser(add_help=False)
        parser = argparse.ArgumentParser(
            add_help=add_help,
            parents=[parent_parser],
            description='Spatial Temporal Graph Convolution Network')

        # region arguments yapf: disable
        # evaluation
        parser.add_argument('--show_topk', type=int, default=[1, 5], nargs='+', help='which Top K accuracy will be shown')
        # optim
        parser.add_argument('--base_lr', type=float, default=0.01, help='initial learning rate')
        parser.add_argument('--step', type=int, default=[], nargs='+', help='the epoch where optimizer reduce the learning rate')
        parser.add_argument('--optimizer', default='SGD', help='type of optimizer')
        parser.add_argument('--nesterov', type=str2bool, default=True, help='use nesterov or not')
        parser.add_argument('--weight_decay', type=float, default=0.0001, help='weight decay for optimizer')
        # endregion yapf: enable

        return parser