import os,sys,json
from argparse import ArgumentParser
import numpy as np
import pandas as pd
import torch
from torch import nn as nn

from torch.nn import functional as F
from torch.utils.data import DataLoader
import torchaudio
from tqdm import tqdm

import importlib
from collections import OrderedDict

from typing import Dict, List, Optional, Callable


from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning import LightningModule, Trainer

from models.extract_feats import PreEmphasis
from models.scores.cosine import cosine_score
class Trainer(LightningModule):
    def __init__(self, models, losses, **kwargs):
        super().__init__()
        self.save_hyperparameters()

        # load trials and data list
        if os.path.exists(self.hparams.trials_path):
            self.trials = np.loadtxt(self.hparams.trials_path, dtype=str)
        if os.path.exists(self.hparams.train_list_path):
            df = pd.read_csv(self.hparams.train_list_path)
            speaker = np.unique(df["utt_spk_int_labels"].values)
            self.hparams.num_classes = len(speaker)
            print("Number of Training Speaker classes is: {}".format(self.hparams.num_classes))

        # Network information Report
        print("Network Type: ", self.hparams.nnet_type)
        print("Pooling Type: ", self.hparams.pooling_type)
        print("Embedding Dim: ", self.hparams.embedding_dim)

        #########################
        ### Network Structure ###
        #########################

        # 1. Acoustic Feature
        self.mel_trans = torch.nn.Sequential(
                PreEmphasis(),
                torchaudio.transforms.MelSpectrogram(sample_rate=16000, n_fft=512, 
                    win_length=400, hop_length=160, window_fn=torch.hamming_window, n_mels=self.hparams.n_mels)
                )
        self.instancenorm = nn.InstanceNorm1d(self.hparams.n_mels)

        # 2. Speaker_Encoder


        
        self.speaker_encoder = models

        # 3. Loss / Classifier
        
       
        self.loss = losses

    def forward(self, x, label):
    
        x = x.reshape(-1, self.hparams.nPerSpeaker, self.hparams.embedding_dim)
        loss, acc = self.loss(x, label)
        return loss.mean(), acc

    def extract_speaker_embedding(self, data):
        x = data.reshape(-1, data.size()[-1])
        x = self.mel_trans(x) + 1e-6
        x = x.log()
        x = self.instancenorm(x)
        x = self.speaker_encoder(x)
        return x

    def training_step(self, batch, batch_idx):
        data, label = batch
        loss, acc = self(data, label)
        tqdm_dict = {"acc":acc}
        self.log('train_loss', loss)
        self.log('train_acc', acc)
        output = OrderedDict({
            'loss': loss,
            'progress_bar': tqdm_dict,
            'log': tqdm_dict
            })
        return output

    def train_dataloader(self):
        frames_len = np.random.randint(self.hparams.min_frames, self.hparams.max_frames)
        print("Chunk size is: ", frames_len)
        print("Augment Mode: ", self.hparams.augment)
        train_dataset = Train_Dataset(self.hparams.train_list_path, self.hparams.augment, 
                musan_list_path=self.hparams.musan_list_path, rirs_list_path=self.hparams.rirs_list_path,
                max_frames=frames_len)
        train_sampler = Train_Sampler(train_dataset, self.hparams.nPerSpeaker,
                self.hparams.max_seg_per_spk, self.hparams.batch_size)
        loader = torch.utils.data.DataLoader(
                train_dataset,
                batch_size=self.hparams.batch_size,
                num_workers=self.hparams.num_workers,
                sampler=train_sampler,
                pin_memory=True,
                drop_last=False,
                )
        return loader

    def test_dataloader(self, trials):
        enroll_list = np.unique(trials.T[1])
        test_list = np.unique(trials.T[2])
        eval_list = np.unique(np.append(enroll_list, test_list))
        print("number of eval: ", len(eval_list))
        print("number of enroll: ", len(enroll_list))
        print("number of test: ", len(test_list))

        test_dataset = Test_Dataset(data_list=eval_list, eval_frames=self.hparams.eval_frames, num_eval=0)
        loader = DataLoader(test_dataset, num_workers=self.hparams.num_workers, batch_size=1)
        return loader

    def cosine_evaluate(self):
        eval_loader = self.test_dataloader(self.trials)
        index_mapping = {}
        eval_vectors = [[] for _ in range(len(eval_loader))]
        print("extract eval speaker embedding...")
        self.speaker_encoder.eval()
        with torch.no_grad():
            for idx, (data, label) in enumerate(tqdm(eval_loader)):
                data = data.permute(1, 0, 2).cuda()
                label = list(label)[0]
                index_mapping[label] = idx
                embedding = self.extract_speaker_embedding(data)
                embedding = torch.mean(embedding, axis=0)
                embedding = embedding.cpu().detach().numpy()
                eval_vectors[idx] = embedding
        eval_vectors = np.array(eval_vectors)
        print("scoring...")
        eer, th, mindcf_e, mindcf_h = cosine_score(self.trials, index_mapping, eval_vectors)
        print("Cosine EER: {:.3f}%  minDCF(0.01): {:.5f}  minDCF(0.001): {:.5f}".format(eer*100, mindcf_e, mindcf_h))
        self.log('cosine_eer', eer*100)
        self.log('minDCF(0.01)', mindcf_e)
        self.log('minDCF(0.001)', mindcf_h)
        return eer, th, mindcf_e, mindcf_h

    def evaluate(self):
        dev_dataset = Dev_Dataset(data_list_path=self.hparams.dev_list_path, eval_frames=self.hparams.eval_frames, num_eval=0)
        dev_loader = DataLoader(dev_dataset, num_workers=self.hparams.num_workers, batch_size=1)

        # first we extract dev speaker embedding
        dev_vectors = []
        dev_labels = []
        print("extract dev speaker embedding...")
        self.speaker_encoder.eval()
        with torch.no_grad():
            for data, label in tqdm(dev_loader):
                length = len(data)
                embedding = self.extract_speaker_embedding(data.cuda())
                embedding = embedding.reshape(length, 1, -1)
                embedding = torch.mean(embedding, axis=1)
                embedding = embedding.cpu().detach().numpy()
                dev_vectors.append(embedding)
                label = label.cpu().detach().numpy()
                dev_labels.append(label)

        dev_vectors = np.vstack(dev_vectors).reshape(-1, self.hparams.embedding_dim)
        dev_labels = np.hstack(dev_labels)
        print("dev vectors shape:", dev_vectors.shape)
        print("dev labels shape:", dev_labels.shape)

        eval_loader = self.test_dataloader(self.trials)
        index_mapping = {}
        eval_vectors = [[] for _ in range(len(eval_loader))]
        print("extract eval speaker embedding...")
        self.speaker_encoder.eval()
        with torch.no_grad():
            for idx, (data, label) in enumerate(tqdm(eval_loader)):
                data = data.permute(1, 0, 2).cuda()
                label = list(label)[0]
                index_mapping[label] = idx
                embedding = self.extract_speaker_embedding(data)
                embedding = torch.mean(embedding, axis=0)
                embedding = embedding.cpu().detach().numpy()
                eval_vectors[idx] = embedding

        eval_vectors = np.array(eval_vectors)
        print("eval_vectors shape is: ", eval_vectors.shape)

        print("scoring...")
        eer, th, mindcf_e, mindcf_h = cosine_score(self.trials, index_mapping, eval_vectors)
        print("Cosine EER: {:.3f}%  minDCF(0.01): {:.5f}  minDCF(0.001): {:.5f}".format(eer*100, mindcf_e, mindcf_h))
        self.log('cosine eer', eer*100)
        self.log('cosine minDCF(0.01)', mindcf_e)
        self.log('cosine minDCF(0.001)', mindcf_h)

        # PLDA
        plda = PldaAnalyzer(n_components=self.hparams.plda_dim)
        plda.fit(dev_vectors, dev_labels, num_iter=10)
        eval_vectors_trans = plda.transform(eval_vectors)
        eer, th, mindcf_e, mindcf_h = PLDA_score(self.trials, index_mapping, eval_vectors_trans, plda)
        print("PLDA EER: {:.3f}%  minDCF(0.01): {:.5f}  minDCF(0.001): {:.5f}".format(eer*100, mindcf_e, mindcf_h))
        self.log('plda eer', eer*100)
        self.log('plda minDCF(0.01)', mindcf_e)
        self.log('plda minDCF(0.001)', mindcf_h)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.hparams.learning_rate)
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=self.hparams.lr_step_size, gamma=self.hparams.lr_gamma)
        print("init {} optimizer with learning rate {}".format("Adam", self.hparams.learning_rate))
        print("init Step lr_scheduler with step size {} and gamma {}".format(self.hparams.lr_step_size, self.hparams.lr_gamma))
        return [optimizer], [lr_scheduler]

    def optimizer_step(self, epoch, batch_idx, optimizer, optimizer_idx,
                   optimizer_closure, on_tpu, using_native_amp, using_lbfgs):
        # warm up learning_rate
        if self.trainer.global_step < 500:
            lr_scale = min(1., float(self.trainer.global_step + 1) / 500.)
            for idx, pg in enumerate(optimizer.param_groups):
                pg['lr'] = lr_scale * self.hparams.learning_rate
        # update params
        optimizer.step(closure=optimizer_closure)
        optimizer.zero_grad()

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser], add_help=False)

        # Data Loader       
        parser.add_argument('--max_frames', type=int, default=201)
        parser.add_argument('--min_frames', type=int, default=200)
        parser.add_argument('--eval_frames', type=int, default=0)
        parser.add_argument('--batch_size', type=int, default=32)
        parser.add_argument('--max_seg_per_spk', type=int, default=500, help='Maximum number of utterances per speaker per epoch')
        parser.add_argument('--nPerSpeaker', type=int, default=1, help='Number of utterances per speaker per batch, only for metric learning based losses');
        parser.add_argument('--num_workers', type=int, default=16)
        parser.add_argument('--augment', action='store_true', default=False)

        # Training details
        parser.add_argument('--max_epoch', type=int, default=500, help='Maximum number of epochs')
        parser.add_argument('--loss_type', type=str, default="softmax")
        parser.add_argument('--nnet_type', type=str, default="ResNetSE34L")
        parser.add_argument('--pooling_type', type=str, default="SAP")
        parser.add_argument('--eval_interval', type=int, default=1)
        parser.add_argument('--keep_loss_weight', action='store_true', default=False)

        # Optimizer
        parser.add_argument('--learning_rate', type=float, default=0.001)
        parser.add_argument('--lr_step_size', type=int, default=10)
        parser.add_argument('--lr_gamma', type=float, default=0.95)

        # Loss functions
        parser.add_argument('--margin', type=float, default=0.2)
        parser.add_argument('--scale', type=float, default=30)

        # Training and test data
        parser.add_argument('--train_list_path', type=str, default='')
        parser.add_argument('--dev_list_path', type=str, default='')
        parser.add_argument('--trials_path', type=str, default='trials.lst')
        parser.add_argument('--musan_list_path', type=str, default='')
        parser.add_argument('--rirs_list_path', type=str, default='')

        # Load and save
        parser.add_argument('--checkpoint_path', type=str, default=None)
        parser.add_argument('--save_top_k', type=int, default=15)
        parser.add_argument('--suffix', type=str, default='')

        # Model definition
        parser.add_argument('--n_mels', type=int, default=40)
        parser.add_argument('--embedding_dim', type=int, default=256)
        parser.add_argument('--plda_dim', type=int, default=256)

        # Test mode
        parser.add_argument('--evaluate', action='store_true', default=False)

        return parser

