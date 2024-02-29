# ------------------------------------------------------------------
# SimpleNet: A Simple Network for Image Anomaly Detection and Localization (https://openaccess.thecvf.com/content/CVPR2023/papers/Liu_SimpleNet_A_Simple_Network_for_Image_Anomaly_Detection_and_Localization_CVPR_2023_paper.pdf)
# Github source: https://github.com/DonaldRR/SimpleNet
# Licensed under the MIT License [see LICENSE for details]
# The script is based on the code of PatchCore (https://github.com/amazon-science/patchcore-inspection)
# ------------------------------------------------------------------

"""detection methods."""
import logging
import os
import pickle
from collections import OrderedDict
from PIL import Image

import math
import numpy as np
import torch
import torch.nn.functional as F
import tqdm
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter
from torch import nn

from torchvision import transforms
from torch.utils.data import DataLoader
from torchvision.datasets import ImageFolder
from torchvision.io import read_image

import common
import metrics

from utils import plot_segmentation_images

from torchvision import transforms, datasets
from torch.utils.data import DataLoader
from itertools import cycle

from models_mae import mae_vit_base_patch16
# --------------------------------------------------------
import torch.multiprocessing as mp
from timm.models.layers import trunc_normal_
import time
import datetime
import math
import sys
from typing import Iterable
from datasets.mvtec import MVTecDataset, DatasetSplit
from mae.util.pos_embed import interpolate_pos_embed
from timm.utils import accuracy

import torch
from models_vit import vit_base_patch16

import mae.util.misc as misc
import mae.util.lr_sched as lr_sched
from mae.util.misc import NativeScalerWithGradNormCount as NativeScaler

import timm
assert timm.__version__ == "0.3.2"  # version check
import timm.optim.optim_factory as optim_factory
from mae.engine_finetune import train_one_epoch, evaluate

from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
import argparse

from tool.patch_embed import PatchEmbedDecoder

patch_embed_params = {
    'img_size': 224,
    'patch_size': 16,
    'in_chans': 3,
    'embed_dim': 768,
    'norm_layer': None,
    'flatten': True,
    'output_fmt': 'NCHW',
    'bias': True,
    'strict_img_size': True,
    'dynamic_img_pad': False,
}


# PatchEmbedDecoder 초기화
patchembeddecoder = PatchEmbedDecoder(**patch_embed_params)

LOGGER = logging.getLogger(__name__)



def init_weight(m):

    if isinstance(m, torch.nn.Linear):
        torch.nn.init.xavier_normal_(m.weight)
    elif isinstance(m, torch.nn.Conv2d):
        torch.nn.init.xavier_normal_(m.weight)



class Discriminator(torch.nn.Module):
    def __init__(self, in_planes, n_layers=1, hidden=None):
        super(Discriminator, self).__init__()

        _hidden = in_planes if hidden is None else hidden
        self.body = torch.nn.Sequential()
        for i in range(n_layers-1):
            _in = in_planes if i == 0 else _hidden
            _hidden = int(_hidden // 1.5) if hidden is None else hidden
            self.body.add_module('block%d'%(i+1),
                                 torch.nn.Sequential(
                                     torch.nn.Linear(_in, _hidden),
                                     torch.nn.BatchNorm1d(_hidden),
                                     torch.nn.LeakyReLU(0.2)
                                 ))
        self.tail = torch.nn.Linear(_hidden, 1, bias=False)
        self.apply(init_weight)

    def forward(self,x):
        x = self.body(x)
        x = self.tail(x)
        return x


class Projection(torch.nn.Module):
    
    def __init__(self, in_planes, out_planes=None, n_layers=1, layer_type=0):
        super(Projection, self).__init__()
        
        if out_planes is None:
            out_planes = in_planes
        self.layers = torch.nn.Sequential()
        _in = None
        _out = None
        for i in range(n_layers):
            _in = in_planes if i == 0 else _out
            _out = out_planes 
            self.layers.add_module(f"{i}fc", 
                                   torch.nn.Linear(_in, _out))
            if i < n_layers - 1:
                # if layer_type > 0:
                #     self.layers.add_module(f"{i}bn", 
                #                            torch.nn.BatchNorm1d(_out))
                if layer_type > 1:
                    self.layers.add_module(f"{i}relu",
                                           torch.nn.LeakyReLU(.2))
        self.apply(init_weight)
    
    def forward(self, x):
        
        # x = .1 * self.layers(x) + x
        x = self.layers(x)
        return x

#### domain classifier 추가 

class DomainClassifier(nn.Module):

    def __init__(self, input_size=1536, output_size=2):
        super(DomainClassifier, self).__init__()

        self.fcD = nn.Sequential(
            nn.Linear(input_size, output_size)
        )

    def forward(self, x):
        #print('x', x[0])
        # 전처리: 2D 텐서로 변환
        x = x.reshape(x.size(0), -1)
        #print('xre', x.shape)
        # 선형 레이어 통과
        x = self.fcD(F.relu(x).to(torch.float32))
        
        return x
    

class Connector(nn.Module):

    def __init__(self):
        super(Connector, self).__init__()

        self.fcD = nn.Sequential(
            # fcD
            nn.Linear(2, 1536)
        )

    def forward(self, x):
        #print('fcD', x.shape)
        x = self.fcD(x)
        #print('later', x.shape)
        return x

class TBWrapper:
    
    def __init__(self, log_dir):
        self.g_iter = 0
        self.logger = SummaryWriter(log_dir=log_dir)
    
    def step(self):
        self.g_iter += 1

# domain adaptation 의 정확도 출력 
def acc_fn(pred, true):
    #accuracy = torch.eq(pred, true).sum().item() / len(pred)
    #print(f"pred size: {pred.size()}")
    #print(f"true size: {true.size()}")
    accuracy = torch.eq(pred.argmax(dim=1), true).sum().item() / len(pred)
    return accuracy
    
class MeanMapperad(torch.nn.Module):
    def __init__(self, preprocessing_dim):
        super(MeanMapperad, self).__init__()
        self.preprocessing_dim = preprocessing_dim

    def forward(self, features):
        features = features.reshape(len(features), 1, -1)
        return F.adaptive_avg_pool1d(features, self.preprocessing_dim).squeeze(1)


class Preprocessingad(torch.nn.Module):
    def __init__(self, input_dims, output_dim):
        super(Preprocessingad, self).__init__()
        self.input_dims = input_dims
        self.output_dim = output_dim

        self.preprocessing_modules = torch.nn.ModuleList()
        for input_dim in input_dims:
            module = MeanMapperad(output_dim)
            self.preprocessing_modules.append(module)

    def forward(self, features):
        _features = []
        for module, feature in zip(self.preprocessing_modules, features):
            _features.append(module(feature))
        return torch.stack(_features, dim=1) #(batch_size, num_input_dims, output_dim)의 크기를 갖게됨


class Aggregatorad(torch.nn.Module):
    def __init__(self, target_dim):
        super(Aggregatorad, self).__init__()
        self.target_dim = target_dim

    def forward(self, features):
        """Returns reshaped and average pooled features."""
        # batchsize x number_of_layers x input_dim -> batchsize x target_dim
        features = features.reshape(len(features), 1, -1)  # 3D로 변환
        features = F.adaptive_avg_pool1d(features, self.target_dim)
        #features = features.squeeze(2)
        return features.reshape(len(features), -1)
    

class SimpleNet(torch.nn.Module):
    def __init__(self, device):
        """anomaly detection class."""
        super(SimpleNet, self).__init__()
        self.device = device

    def load(
        self,
        backbone,
        layers_to_extract_from,
        device,
        input_shape,
        pretrain_embed_dimension, # 1536
        target_embed_dimension, # 1536
        patchsize=3, # 3
        patchstride=1, 
        embedding_size=None, # 256
        meta_epochs=1, # 40
        aed_meta_epochs=1,
        gan_epochs=1, # 4
        noise_std=0.05,
        mix_noise=1,
        noise_type="GAU",
        dsc_layers=2, # 2
        dsc_hidden=None, # 1024
        dsc_margin=.8, # .5
        dsc_lr=0.0002,
        train_backbone=False,
        auto_noise=0,
        cos_lr=False,
        lr=1e-3,
        pre_proj=0, # 1
        proj_layer_type=0,
        **kwargs,
    ):
        
        
        pid = os.getpid()
        def show_mem():
            return(psutil.Process(pid).memory_info())
        

        self.backbone = backbone.to(device)
        self.layers_to_extract_from = layers_to_extract_from
        self.input_shape = input_shape

        self.device = device
        self.patch_maker = PatchMaker(patchsize, stride=patchstride)

        self.forward_modules = torch.nn.ModuleDict({})

        feature_aggregator = common.NetworkFeatureAggregator( #patchcore 방식으로 feature 를 추출하는 방법
            self.backbone, self.layers_to_extract_from, self.device, train_backbone
        )
        feature_dimensions = feature_aggregator.feature_dimensions(input_shape) #input-size 값으로 feature 크기를 맞춰줌
        self.forward_modules["feature_aggregator"] = feature_aggregator

        preprocessing = common.Preprocessing(
            feature_dimensions, pretrain_embed_dimension
        )
        self.forward_modules["preprocessing"] = preprocessing

        self.target_embed_dimension = target_embed_dimension
        preadapt_aggregator = common.Aggregator( #타겟 도메인에 feature 를 맞추는 역할 
            target_dim=target_embed_dimension
        )

        _ = preadapt_aggregator.to(self.device)

        self.forward_modules["preadapt_aggregator"] = preadapt_aggregator

        self.anomaly_segmentor = common.RescaleSegmentor(
            device=self.device, target_size=input_shape[-2:]
        )

        self.embedding_size = embedding_size if embedding_size is not None else self.target_embed_dimension
        self.meta_epochs = meta_epochs
        self.lr = lr
        self.cos_lr = cos_lr
        self.train_backbone = train_backbone
        if self.train_backbone:
            self.backbone_opt = torch.optim.AdamW(self.forward_modules["feature_aggregator"].backbone.parameters(), lr)
        # AED
        self.aed_meta_epochs = aed_meta_epochs

        self.pre_proj = pre_proj
        if self.pre_proj > 0:
            self.pre_projection = Projection(self.target_embed_dimension, self.target_embed_dimension, pre_proj, proj_layer_type)
            self.pre_projection.to(self.device)
            self.proj_opt = torch.optim.AdamW(self.pre_projection.parameters(), lr*.1)

        self.preprocessingad = Preprocessingad(
            feature_dimensions, pretrain_embed_dimension
        )
        self.meanmapperad = MeanMapperad
        self.aggregatorad = Aggregatorad( #타겟 도메인에 feature 를 맞추는 역할 
            target_dim=target_embed_dimension
        )
        # Discriminator
        self.auto_noise = [auto_noise, None]
        self.dsc_lr = dsc_lr
        self.gan_epochs = gan_epochs
        self.mix_noise = mix_noise
        self.noise_type = noise_type
        self.noise_std = noise_std
        self.discriminator = Discriminator(self.target_embed_dimension, n_layers=dsc_layers, hidden=dsc_hidden)
        self.discriminator.to(self.device)
        #self.discriminator.to(rank)
        #self.discriminator = DDP(self.discriminator, device_ids=[rank])
        self.dsc_opt = torch.optim.Adam(self.discriminator.parameters(), lr=self.dsc_lr, weight_decay=1e-5)
        self.dsc_schl = torch.optim.lr_scheduler.CosineAnnealingLR(self.dsc_opt, (meta_epochs - aed_meta_epochs) * gan_epochs, self.dsc_lr*.4)
        self.dsc_margin= dsc_margin 

        self.model_dir = ""
        self.dataset_name = ""
        self.tau = 1
        self.logger = None
        


    def set_model_dir(self, model_dir, dataset_name):

        self.model_dir = model_dir 
        os.makedirs(self.model_dir, exist_ok=True)
        self.ckpt_dir = os.path.join(self.model_dir, dataset_name)
        os.makedirs(self.ckpt_dir, exist_ok=True)
        self.tb_dir = os.path.join(self.ckpt_dir, "tb")
        os.makedirs(self.tb_dir, exist_ok=True)
        self.logger = TBWrapper(self.tb_dir) #SummaryWriter(log_dir=tb_dir)

    def mae_model_dir(self, model_dir, dataset_name):
            self.mae_model_dir = model_dir 
            os.makedirs(self.mae_model_dir, exist_ok=True)
            self.mae_ckpt_dir = os.path.join(self.mae_model_dir, dataset_name)
            os.makedirs(self.mae_ckpt_dir, exist_ok=True)
            self.mae_tb_dir = os.path.join(self.mae_ckpt_dir, "tb")
            os.makedirs(self.mae_tb_dir, exist_ok=True)
            self.mae_logger = TBWrapper(self.mae_tb_dir) #SummaryWriter(log_dir=tb_dir)


    def ad_model_dir(self, model_dir, dataset_name):
            self.ad_model_dir = model_dir 
            os.makedirs(self.ad_model_dir, exist_ok=True)
            self.ad_ckpt_dir = os.path.join(self.ad_model_dir, dataset_name)
            os.makedirs(self.ad_ckpt_dir, exist_ok=True)
            self.ad_tb_dir = os.path.join(self.ad_ckpt_dir, "tb")
            os.makedirs(self.ad_tb_dir, exist_ok=True)
            self.ad_logger = TBWrapper(self.ad_tb_dir) #SummaryWriter(log_dir=tb_dir)

    def embed(self, data):

        if isinstance(data, torch.utils.data.DataLoader):
            features = []
            for image in data:
                if isinstance(image, dict):
                    image = image["image"]
                    input_image = image.to(torch.float).to(self.device)
                
                features.append(self._embed(input_image))
            return features
        return self._embed(data)

    def _embed(self, images, detach=True, provide_patch_shapes=False, evaluation=False):
        """Returns feature embeddings for images."""



        B = len(images)
        if not evaluation and self.train_backbone:
            self.forward_modules["feature_aggregator"].train() #여기서 피처값 뽑히고 학습 진행 (프리트레인된 모델로)
            features = self.forward_modules["feature_aggregator"](images, eval=evaluation)
        else:
            _ = self.forward_modules["feature_aggregator"].eval()
            with torch.no_grad():
                features = self.forward_modules["feature_aggregator"](images)

        features = [features[layer] for layer in self.layers_to_extract_from]
   

        for i, feat in enumerate(features):
            if len(feat.shape) == 3:
                B, L, C = feat.shape
                features[i] = feat.reshape(B, int(math.sqrt(L)), int(math.sqrt(L)), C).permute(0, 3, 1, 2)

        features = [
            self.patch_maker.patchify(x, return_spatial_info=True) for x in features
        ]
   
        patch_shapes = [x[1] for x in features]
        features = [x[0] for x in features]
        ref_num_patches = patch_shapes[0]

        for i in range(1, len(features)):
            _features = features[i]
            patch_dims = patch_shapes[i]

            # TODO(pgehler): Add comments
            _features = _features.reshape(
                _features.shape[0], patch_dims[0], patch_dims[1], *_features.shape[2:]
            )
           
            _features = _features.permute(0, -3, -2, -1, 1, 2)
            
            perm_base_shape = _features.shape
            _features = _features.reshape(-1, *_features.shape[-2:])
           
            _features = F.interpolate(
                _features.unsqueeze(1),
                size=(ref_num_patches[0], ref_num_patches[1]),
                mode="bilinear",
                align_corners=False,
            )
            _features = _features.squeeze(1)
            _features = _features.reshape(
                *perm_base_shape[:-2], ref_num_patches[0], ref_num_patches[1]
            )
            _features = _features.permute(0, -2, -1, 1, 2, 3)
            _features = _features.reshape(len(_features), -1, *_features.shape[-3:])
            features[i] = _features
            
        features = [x.reshape(-1, *x.shape[-3:]) for x in features]
      
        # As different feature backbones & patching provide differently
        # sized features, these are brought into the correct form here.
        features = self.forward_modules["preprocessing"](features) # pooling each feature to same channel and stack together
        features = self.forward_modules["preadapt_aggregator"](features) # further pooling   


        return features, patch_shapes
    
    #############################domain adaptation 을 위한 embed 진행(target size 에 맞춰주는 과정 X)##################

    def daembed(self, data):

        if isinstance(data, torch.utils.data.DataLoader):
            features = []
            for image in data:
                if isinstance(image, dict):
                    image = image["image"]
                    input_image = image.to(torch.float).to(self.device)
                
                features.append(self.da_embed(input_image))
            return features
        return self.da_embed(data)

    def da_embed(self, images, model, detach=True, provide_patch_shapes=False, evaluation=False):

        #print(images.shape)

        """이미 계산된 특성에 대한 feature embedding을 반환"""
        output,intermediate_outputs = model.forward_features(images)

        intermediate_outputs = [patchembeddecoder(feat) for feat in intermediate_outputs]
        features = intermediate_outputs


        #features = [features[layer] for layer in self.layers_to_extract_from]
        #_, features = model.blocks[-1](images)
        # output = model.forward_features(images)
        # features = model.intermediate_output
        #print(features.shape)

        #print('1', len(features[0]), len(features[1]))

        for i, feat in enumerate(features):
            if len(feat.shape) == 3:
                B, L, C = feat.shape
                token_features = [feat[:, j, :].view(B, 1, C) for j in range(L)]
                features[i] = token_features

        # 추가 부분: 3D 텐서를 2D로 변환
        features = [torch.cat([f.view(B, 1, C) for f in feat], dim=1) for feat in features]
        features = [feat.permute(0, 2, 1) for feat in features]
        #print('2', len(features[0]), len(features[1]))

        features = [feat.unsqueeze(2) for feat in features]
        features = [feat.permute(0, 3, 1, 2) for feat in features]
        features = [feat.reshape(B, -1, C,1) for feat in features]
        
        features = [
            self.patch_maker.patchify(x, return_spatial_info=True) for x in features
        ]
        # token_features_concatenated = torch.cat([torch.cat(token_features, dim=2).unsqueeze(3) for token_features in features], dim=2)
        # print('4', token_features_concatenated.shape)
        # features = [self.patch_maker.patchify(x, return_spatial_info=True) for x in token_features_concatenated]
        patch_shapes = [x[1] for x in features]
        features = [x[0] for x in features]
        ref_num_patches = patch_shapes[0]
        #print('5', len(features[0]), len(features[1]))

        for i in range(1, len(features)):
            _features = features[i]
            patch_dims = patch_shapes[i]

            _features = _features.reshape(
                _features.shape[0], patch_dims[0], patch_dims[1], *_features.shape[2:]
            )

            _features = _features.permute(0, -3, -2, -1, 1, 2)

            perm_base_shape = _features.shape
            _features = _features.reshape(-1, *_features.shape[-2:])

            _features = F.interpolate(
                _features.unsqueeze(1),
                size=(ref_num_patches[0], ref_num_patches[1]),
                mode="bilinear",
                align_corners=False,
            )
            _features = _features.squeeze(1)
            _features = _features.reshape(
                *perm_base_shape[:-2], ref_num_patches[0], ref_num_patches[1]
            )
            _features = _features.permute(0, -2, -1, 1, 2, 3)
            _features = _features.reshape(len(_features), -1, *_features.shape[-3:])
            features[i] = _features
        #print('6', len(features[0]), len(features[1]))
        #features = [x.reshape(B, -1, *x.shape[-3:]) for x in features]
        features = [x.reshape(-1, *x.shape[-3:]) for x in features]  # 변경된 부분
        #print('7', len(features[0]), len(features[1]))
        features = self.preprocessingad(features)
        features = self.aggregatorad(features)
        #print('aggregator before', features.shape)

        return features, patch_shapes
    
    
    def test(self, training_data, test_data, save_segmentation_images):

 
        ckpt_path = os.path.join(self.ckpt_dir, "ckpt.pth")


        if os.path.exists(ckpt_path):
            state_dicts = torch.load(ckpt_path, map_location=self.device)
           
            if "discriminator" in state_dicts:
	            self.discriminator.load_state_dict(state_dicts['discriminator'])

            if "pre_projection" in state_dicts:
	            self.pre_projection.load_state_dict(state_dicts["pre_projection"])
            
            else:
                self.load_state_dict(state_dicts, strict=False)
        

        aggregator = {"scores": [], "segmentations": [], "features": []}
        scores, segmentations, features, labels_gt, masks_gt = self.predict(test_data)
        aggregator["scores"].append(scores)
        aggregator["segmentations"].append(segmentations)
        aggregator["features"].append(features)

        scores = np.array(aggregator["scores"])
        min_scores = scores.min(axis=-1).reshape(-1, 1)
        max_scores = scores.max(axis=-1).reshape(-1, 1)
        scores = (scores - min_scores) / (max_scores - min_scores)
        scores = np.mean(scores, axis=0)

        segmentations = np.array(aggregator["segmentations"])
        min_scores = (
            segmentations.reshape(len(segmentations), -1)
            .min(axis=-1)
            .reshape(-1, 1, 1, 1)
        )
        max_scores = (
            segmentations.reshape(len(segmentations), -1)
            .max(axis=-1)
            .reshape(-1, 1, 1, 1)
        )
        segmentations = (segmentations - min_scores) / (max_scores - min_scores)
        segmentations = np.mean(segmentations, axis=0)

        anomaly_labels = [
            x[1] != "good" for x in test_data.dataset.data_to_iterate
        ]

        if save_segmentation_images:
            self.save_segmentation_images(test_data, segmentations, scores)
            
        auroc = metrics.compute_imagewise_retrieval_metrics(
            scores, anomaly_labels
        )["auroc"]

        # Compute PRO score & PW Auroc for all images
        pixel_scores = metrics.compute_pixelwise_retrieval_metrics(
            segmentations, masks_gt
        )
        full_pixel_auroc = pixel_scores["auroc"]


        return auroc, full_pixel_auroc , 1
    
    def _evaluate(self, test_data, scores, segmentations, features, labels_gt, masks_gt):
        

        scores = np.squeeze(np.array(scores))
        img_min_scores = scores.min(axis=-1)
        img_max_scores = scores.max(axis=-1)
        scores = (scores - img_min_scores) / (img_max_scores - img_min_scores)
        # scores = np.mean(scores, axis=0)

        auroc = metrics.compute_imagewise_retrieval_metrics(
            scores, labels_gt 
        )["auroc"]

        if len(masks_gt) > 0:
            segmentations = np.array(segmentations)
            min_scores = (
                segmentations.reshape(len(segmentations), -1)
                .min(axis=-1)
                .reshape(-1, 1, 1, 1)
            )
            max_scores = (
                segmentations.reshape(len(segmentations), -1)
                .max(axis=-1)
                .reshape(-1, 1, 1, 1)
            )
            norm_segmentations = np.zeros_like(segmentations)
            for min_score, max_score in zip(min_scores, max_scores):
                norm_segmentations += (segmentations - min_score) / max(max_score - min_score, 1e-2)
            norm_segmentations = norm_segmentations / len(scores)


            # Compute PRO score & PW Auroc for all images
            pixel_scores = metrics.compute_pixelwise_retrieval_metrics(
                norm_segmentations, masks_gt)
                # segmentations, masks_gt
            full_pixel_auroc = pixel_scores["auroc"]

            pro = metrics.compute_pro(np.squeeze(np.array(masks_gt)), 
                                            norm_segmentations)
        else:
            full_pixel_auroc = -1 
            pro = -1

        return auroc, full_pixel_auroc, pro
        
    
    def train(self, training_data, test_data):

        state_dict = {}
        ckpt_path = os.path.join(self.ckpt_dir, "ckpt.pth")
        if os.path.exists(ckpt_path):
            state_dict = torch.load(ckpt_path, map_location=self.device)
            if 'discriminator' in state_dict:
                self.discriminator.load_state_dict(state_dict['discriminator'])
                if "pre_projection" in state_dict:
                    self.pre_projection.load_state_dict(state_dict["pre_projection"])
            else:
                self.load_state_dict(state_dict, strict=False)

            self.predict(training_data, "train_")

            scores, segmentations, features, labels_gt, masks_gt = self.predict(test_data)
            auroc, full_pixel_auroc, anomaly_pixel_auroc = self._evaluate(test_data, scores, segmentations, features, labels_gt, masks_gt)
            
            return auroc, full_pixel_auroc, anomaly_pixel_auroc
        
        def update_state_dict(d):
            
            state_dict["discriminator"] = OrderedDict({
                k:v.detach().cpu()  #그래디언트를 분리하고 
                for k, v in self.discriminator.state_dict().items()}) #각각을 딕셔너리에 저장 
            if self.pre_proj > 0:
                state_dict["pre_projection"] = OrderedDict({
                    k:v.detach().cpu() 
                    for k, v in self.pre_projection.state_dict().items()})

        best_record = None
        for i_mepoch in range(self.meta_epochs):


            self._train_discriminator(training_data)

            # torch.cuda.empty_cache()
            scores, segmentations, features, labels_gt, masks_gt = self.predict(test_data)
            auroc, full_pixel_auroc, pro = self._evaluate(test_data, scores, segmentations, features, labels_gt, masks_gt)
            self.logger.logger.add_scalar("i-auroc", auroc, i_mepoch)
            self.logger.logger.add_scalar("p-auroc", full_pixel_auroc, i_mepoch)
            self.logger.logger.add_scalar("pro", pro, i_mepoch)

            if best_record is None:
                best_record = [auroc, full_pixel_auroc, pro]
                update_state_dict(state_dict)
                # state_dict = OrderedDict({k:v.detach().cpu() for k, v in self.state_dict().items()})
            else:
                if auroc > best_record[0]:
                    best_record = [auroc, full_pixel_auroc, pro]
                    update_state_dict(state_dict)
                    # state_dict = OrderedDict({k:v.detach().cpu() for k, v in self.state_dict().items()})
                elif auroc == best_record[0] and full_pixel_auroc > best_record[1]:
                    best_record[1] = full_pixel_auroc
                    best_record[2] = pro 
                    update_state_dict(state_dict)
                    # state_dict = OrderedDict({k:v.detach().cpu() for k, v in self.state_dict().items()})

            print(f"----- {i_mepoch} I-AUROC:{round(auroc, 4)}(MAX:{round(best_record[0], 4)})"
                  f"  P-AUROC{round(full_pixel_auroc, 4)}(MAX:{round(best_record[1], 4)}) -----"
                  f"  PRO-AUROC{round(pro, 4)}(MAX:{round(best_record[2], 4)}) -----")
        
        torch.save(state_dict, ckpt_path)
        
        return best_record
    #################################################domain classifier 정의##############################################################

    def save_classifier_weights(self, model, save_path):
        state_dict = OrderedDict((k, v.detach().cpu()) for k, v in model.state_dict().items())
        torch.save(state_dict, save_path)

        return state_dict

    def _train_discriminator(self, input_data):
        """Computes and sets the support features for SPADE."""
        

        tgt_dir = '/home/smk/data/project/MVTec'
        src_dir = '/home/smk/data/dataset/imagenet-sample-images-master/'


        #MVTec 전처리
        for data_item in input_data:

            tgt_data = data_item["image"]
            tgt_data = tgt_data.to(torch.float).to(self.device)
            

        #src_data 전처리
        transform_train = transforms.Compose([
            transforms.RandomResizedCrop(224, scale=(0.2, 1.0), interpolation=3),  # 3 is bicubic
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
        dataset_src = datasets.ImageFolder(os.path.join(src_dir), transform=transform_train)
        #dataset_src = CustomImageDataset(root_dir=os.path.join(src_dir), transform=transform_train)
        dataset_tgt = datasets.ImageFolder(os.path.join(tgt_dir), transform=transform_train)
        
        #DA 를 위한 전처리단계
        src_data_tensor = torch.stack([sample[0] for sample in dataset_src])
        src_data = src_data_tensor.to(torch.float).to(self.device)
        #print( isinstance(src_data, list) )

        #MAE 수행하기 위한 선언문
        # num_tasks = misc.get_world_size()
        # global_rank = misc.get_rank()

        # sampler_src = torch.utils.data.DistributedSampler(
        #     dataset_src, num_replicas=num_tasks, rank=global_rank, shuffle=True
        # )

        # sampler_tgt = torch.utils.data.DistributedSampler(
        #     dataset_tgt, num_replicas=num_tasks, rank=global_rank, shuffle=True
        # )

        sampler_src = torch.utils.data.SequentialSampler(dataset_src)
        sampler_tgt = torch.utils.data.SequentialSampler(dataset_tgt)

        
        src_train_loader = torch.utils.data.DataLoader(
                dataset_src, sampler=sampler_src,
                batch_size=16,
                num_workers=1,
                pin_memory='store_true',
                drop_last=True,
        )

        tgt_train_loader = torch.utils.data.DataLoader(
                dataset_tgt, sampler=sampler_tgt,
                batch_size=16,
                num_workers=1,
                pin_memory='store_true',
                drop_last=True,
        )

        
        #tgt_train_loader = torch.utils.data.DataLoader(tgt_data, batch_size=8, shuffle=True)

        
        model = vit_base_patch16(drop_path_rate=0.1,global_pool=True)

        # model = model.to(self.device_id)
        # model = DDP(model, device_ids=[self.device_id])
        #model = vit_base_patch16(num_classes=1000,drop_path_rate=0.1,global_pool=True)
        #model.to(self.device)

        #####################fine tuning code########################
        finetune = '/home/smk/data/project/MAE_DA_AD/mae/checkpoint/mae_finetuned_vit_base.pth'

        checkpoint = torch.load(finetune, map_location=self.device)
        
        print("Load pre-trained checkpoint from: %s" % finetune)
        checkpoint_model = checkpoint['model']
        state_dict =model.state_dict()
        # for key in checkpoint_model.keys():
        #     print(key)

        for k in ['head.weight', 'head.bias']:
            # if k in checkpoint_model:
            #     print(f"Removing key {k} from pretrained checkpoint")
            #     del checkpoint_model[k]
            # else:
            #       print(f"Key {k} not found in pretrained checkpoint")
            if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
                print(f"Removing key {k} from pretrained checkpoint")
                del checkpoint_model[k]

        # interpolate position embedding
        interpolate_pos_embed(model, checkpoint_model)

        # load pre-trained model
        msg = model.load_state_dict(checkpoint_model, strict=False)
        print(msg)


        expected_missing_keys = {'head.weight', 'head.bias', 'fc_norm.weight', 'fc_norm.bias'}
        actual_missing_keys = set(msg.missing_keys)

        assert actual_missing_keys.issubset(expected_missing_keys), f"Unexpected missing keys: {actual_missing_keys}"

        trunc_normal_(model.head.weight, std=2e-5)

        model.to(self.device)

        n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
        #print("Model = %s" % str(model))
        print('number of params (M): %.2f' % (n_parameters / 1.e6))


        batch_size = 16
        blr = 1e-3
        criterion = nn.CrossEntropyLoss().to(self.device)
        param_groups = optim_factory.add_weight_decay(model, 0.05)
        epochs = 400
        model.train(True)
        metric_logger = misc.MetricLogger(delimiter="  ")
        metric_logger.add_meter('lr', misc.SmoothedValue(window_size=1, fmt='{value:.6f}'))
        #model = model.to(rank)
        #model = DDP(model, device_ids=[rank])

        loss_scaler = NativeScaler()
        log_writer = SummaryWriter(log_dir = '/home/smk/data/project/MAE_DA_AD/log_real')

        if log_writer is not None:
            print('log_dir: {}'.format(log_writer.log_dir))

        #####################domain adaptation code########################
        dm_classifier = DomainClassifier().to(self.device)

        dm_classifier = dm_classifier.to(torch.float)
        #dm_classifier = DDP(dm_classifier, device_ids=[rank])

        lam =  0.01
        momentum = 0.9
        lr_d = 1e-3
        criterion = nn.CrossEntropyLoss().to(self.device)
        epochs = 400
        optimizer_dm = optim.SGD( #fcD 학습할 때 사용하는 옵티마이저 정의 
            dm_classifier.parameters(),
            lr=lr_d,
            momentum=momentum) 

        #fix 시켜놓고 모델 사용하기#
        
        header = 'Test:'

        torch.no_grad()
        metric_logger = misc.MetricLogger(delimiter="  ")
        model.eval()

        print('training src dataset')

        #####################DA########################
        print('domain adaptation 시작 !!!')
        with tqdm.tqdm(total=self.gan_epochs) as pbar:
            for i_epoch in range(1, epochs+1):
                for src_data, tgt_data  in zip(src_train_loader, tgt_train_loader):
                    #print('domain adaptation 시작 !!!')
                    #print(src_data.shape)
                    #print(tgt_data.shape)
                    # print(src_data[0].shape)
                    # print(src_data[1].shape)
                    optimizer_dm.zero_grad()

                    src, _ = src_data
                    src = src.to(torch.float).to(self.device)

                    tgt, _ = tgt_data
                    tgt = tgt.to(torch.float).to(self.device)


                    with torch.no_grad():
                        # _, src_feature, _ = model(src_data[0])
                        # _, tgt_feature, _ = model(tgt_data[1])
                        
                        # outputs,src_intermediate_outputs = model.forward_features(src)
                        # src_intermediate_outputs = [patchembeddecoder(feat) for feat in src_intermediate_outputs]
                        # src_feature = src_intermediate_outputs[0]

                        #src_feature = model(src_data[0].half().to(self.device))

                        # output,tgt_intermediate_outputs = model.forward_features(tgt)
                        # tgt_intermediate_outputs = [patchembeddecoder(feat) for feat in tgt_intermediate_outputs]
                        # tgt_feature = tgt_intermediate_outputs[0]

                        # print('src_feature', src_feature.shape)
                        # print('tgt_feature', tgt_feature.shape)


                        #tgt_feature = model(tgt_data[0].half().to(self.device))
                        


                        tgt_feature = self.da_embed(tgt, model, evaluation=False)[0]
                        src_feature = self.da_embed(src, model, evaluation=False)[0]
                    #tgt_feature, _ = self.domainadapt_embed(tgt_data.to(self.device), evaluation=False)

                    src_label_dm = torch.ones(src_feature.shape[0]).to(self.device).long()
                    tgt_label_dm = torch.zeros(tgt_feature.shape[0]).to(self.device).long()
                    #print('src_feature', src_feature.shape)
                    #print('tgt_feature', tgt_feature.shape)
                
                    optimizer_dm.zero_grad()
                    # domain output



                    src_output_dm = dm_classifier(src_feature.detach())
                    #print('src_output', src_output_dm.shape)
                    #print('src_label_dm', src_label_dm.shape)
                    #print('tgt_label_dm', tgt_label_dm.shape)
                    tgt_output_dm = dm_classifier(tgt_feature.detach())
                    #print('tgt_output', tgt_output_dm.shape)

                    src_data, src_label_dm = src_data[0].to(self.device), src_label_dm.to(self.device)
                    tgt_data, tgt_label_dm = tgt_data[0].to(self.device), tgt_label_dm.to(self.device)
                    loss_dm_src = criterion(src_output_dm, src_label_dm)
                    loss_dm_tgt = criterion(tgt_output_dm, tgt_label_dm)
                    loss_dm = lam * (loss_dm_src + loss_dm_tgt)
                    loss_dm.backward()
                    optimizer_dm.step()

                    
                #######ACC, LOSS PRINT METRIC###########

                acc = acc_fn(tgt_output_dm, tgt_label_dm)
                acc_tensor = torch.tensor(acc)

                #pbar.set_description(f"ACC {acc}")
                pbar.set_description(f"Epoch {i_epoch}")
                pbar.set_postfix(loss_dm=loss_dm.item(), acc=acc_tensor.item())
                #pbar.set_postfix(loss_dm=loss_dm.item())
                pbar.update(1)
                # 정확도 기록
                self.ad_logger.logger.add_scalar('Loss', loss_dm.item(), global_step=i_epoch)
                self.ad_logger.logger.add_scalar('Accuracy', acc, global_step=i_epoch)
                # writer.add_scalar('Loss', loss_dm.item(), global_step=i_epoch)
                # writer.add_scalar('Accuracy', acc, global_step=i_epoch)
    

        self.save_classifier_weights(dm_classifier, "/home/smk/data/project/SimpleNetrevised/domainresults/domainresults.pth")





        _ = self.forward_modules.eval()
        
        if self.pre_proj > 0:
            self.pre_projection.train()
        self.discriminator.train()
        # self.feature_enc.eval()
        # self.feature_dec.eval()
        i_iter = 0
        LOGGER.info(f"Training discriminator...")
        with tqdm.tqdm(total=self.gan_epochs) as pbar:
            for i_epoch in range(self.gan_epochs):
                all_loss = []
                all_p_true = []
                all_p_fake = []
                all_p_interp = []
                embeddings_list = []
                for data_item in input_data:
                    self.dsc_opt.zero_grad()
                    if self.pre_proj > 0:
                        self.proj_opt.zero_grad()
                    # self.dec_opt.zero_grad()

                    i_iter += 1
                    ##학습한 img 가져오기
                    #print(img.shape) #[8,3,288,288]
                    
                    if self.pre_proj > 0:
                        true_feats = tgt_feature
                    # if self.pre_proj > 0:
                    #     true_feats = self.pre_projection(self.domainadapt_embed(img, evaluation=False)[0])
                        
                    # else:
                    #     true_feats = self.domainadapt_embed(img, evaluation=False)[0] #original : 10368, 1536]
                        
                    
                    #print(true_feats.shape) #[10368,1536]
                    noise_idxs = torch.randint(0, self.mix_noise, torch.Size([true_feats.shape[0]]))
                    noise_one_hot = torch.nn.functional.one_hot(noise_idxs, num_classes=self.mix_noise).to(self.device) # (N, K)
                    noise = torch.stack([
                        torch.normal(0, self.noise_std * 1.1**(k), true_feats.shape)
                        for k in range(self.mix_noise)], dim=1).to(self.device) # (N, K, C)
                    noise = (noise * noise_one_hot.unsqueeze(-1)).sum(1)
                    fake_feats = true_feats + noise

                    #print(true_feats.shape, fake_feats.shape)

                    #concatenated_feats = torch.cat([true_feats, fake_feats])
                    #print(concatenated_feats.shape)  # 확인용 출력

                    

                    scores = self.discriminator(torch.cat([true_feats, fake_feats]))
                    true_scores = scores[:len(true_feats)]
                    fake_scores = scores[len(fake_feats):]
                    
                    th = self.dsc_margin
                    p_true = (true_scores.detach() >= th).sum() / len(true_scores)
                    p_fake = (fake_scores.detach() < -th).sum() / len(fake_scores)
                    true_loss = torch.clip(-true_scores + th, min=0)
                    fake_loss = torch.clip(fake_scores + th, min=0)

                    self.logger.logger.add_scalar(f"p_true", p_true, self.logger.g_iter)
                    self.logger.logger.add_scalar(f"p_fake", p_fake, self.logger.g_iter)

                    loss = true_loss.mean() + fake_loss.mean()
                    self.logger.logger.add_scalar("loss", loss, self.logger.g_iter)
                    self.logger.step()

                    loss.backward()
                    # if self.pre_proj > 0:
                    #     self.proj_opt.step()
                    if self.train_backbone:
                        self.backbone_opt.step()
                    self.dsc_opt.step()

                    loss = loss.detach().cpu() 
                    all_loss.append(loss.item())
                    all_p_true.append(p_true.cpu().item())
                    all_p_fake.append(p_fake.cpu().item())
                
                if len(embeddings_list) > 0:
                    self.auto_noise[1] = torch.cat(embeddings_list).std(0).mean(-1)
                
                if self.cos_lr:
                    self.dsc_schl.step()
                
                all_loss = sum(all_loss) / len(input_data)
                all_p_true = sum(all_p_true) / len(input_data)
                all_p_fake = sum(all_p_fake) / len(input_data)
                cur_lr = self.dsc_opt.state_dict()['param_groups'][0]['lr']
                pbar_str = f"epoch:{i_epoch} loss:{round(all_loss, 5)} "
                pbar_str += f"lr:{round(cur_lr, 6)}"
                pbar_str += f" p_true:{round(all_p_true, 3)} p_fake:{round(all_p_fake, 3)}"
                if len(all_p_interp) > 0:
                    pbar_str += f" p_interp:{round(sum(all_p_interp) / len(input_data), 3)}"
                pbar.set_description_str(pbar_str)
                pbar.update(1)


    def predict(self, data, prefix=""):
        if isinstance(data, torch.utils.data.DataLoader):
            return self._predict_dataloader(data, prefix)
        return self._predict(data)

    def _predict_dataloader(self, dataloader, prefix):
        """This function provides anomaly scores/maps for full dataloaders."""
        _ = self.forward_modules.eval()


        img_paths = []
        scores = []
        masks = []
        features = []
        labels_gt = []
        masks_gt = []
        from sklearn.manifold import TSNE

        with tqdm.tqdm(dataloader, desc="Inferring...", leave=False) as data_iterator:
            for data in data_iterator:
                if isinstance(data, dict):
                    labels_gt.extend(data["is_anomaly"].numpy().tolist())
                    if data.get("mask", None) is not None:
                        masks_gt.extend(data["mask"].numpy().tolist())
                    image = data["image"]
                    img_paths.extend(data['image_path'])
                _scores, _masks, _feats = self._predict(image)
                for score, mask, feat, is_anomaly in zip(_scores, _masks, _feats, data["is_anomaly"].numpy().tolist()):
                    scores.append(score)
                    masks.append(mask)

        return scores, masks, features, labels_gt, masks_gt

    def _predict(self, images):
        """Infer score and mask for a batch of images."""
        images = images.to(torch.float).to(self.device)
        _ = self.forward_modules.eval()

        batchsize = images.shape[0]
        if self.pre_proj > 0:
            self.pre_projection.eval()
        self.discriminator.eval()
        with torch.no_grad():
            features, patch_shapes = self._embed(images,
                                                 provide_patch_shapes=True, 
                                                 evaluation=True)
            if self.pre_proj > 0:
                features = self.pre_projection(features)

            # features = features.cpu().numpy()
            # features = np.ascontiguousarray(features.cpu().numpy())
            patch_scores = image_scores = -self.discriminator(features)
            patch_scores = patch_scores.cpu().numpy()
            image_scores = image_scores.cpu().numpy()

            image_scores = self.patch_maker.unpatch_scores(
                image_scores, batchsize=batchsize
            )
            image_scores = image_scores.reshape(*image_scores.shape[:2], -1)
            image_scores = self.patch_maker.score(image_scores)

            patch_scores = self.patch_maker.unpatch_scores(
                patch_scores, batchsize=batchsize
            )
            scales = patch_shapes[0]
            patch_scores = patch_scores.reshape(batchsize, scales[0], scales[1])
            features = features.reshape(batchsize, scales[0], scales[1], -1)
            masks, features = self.anomaly_segmentor.convert_to_segmentation(patch_scores, features)

        return list(image_scores), list(masks), list(features)

    @staticmethod
    def _params_file(filepath, prepend=""):
        return os.path.join(filepath, prepend + "params.pkl")

    def save_to_path(self, save_path: str, prepend: str = ""):
        LOGGER.info("Saving data.")
        self.anomaly_scorer.save(
            save_path, save_features_separately=False, prepend=prepend
        )
        params = {
            "backbone.name": self.backbone.name,
            "layers_to_extract_from": self.layers_to_extract_from,
            "input_shape": self.input_shape,
            "pretrain_embed_dimension": self.forward_modules[
                "preprocessing"
            ].output_dim,
            "target_embed_dimension": self.forward_modules[
                "preadapt_aggregator"
            ].target_dim,
            "patchsize": self.patch_maker.patchsize,
            "patchstride": self.patch_maker.stride,
            "anomaly_scorer_num_nn": self.anomaly_scorer.n_nearest_neighbours,
        }
        with open(self._params_file(save_path, prepend), "wb") as save_file:
            pickle.dump(params, save_file, pickle.HIGHEST_PROTOCOL)

    def save_segmentation_images(self, data, segmentations, scores):
        image_paths = [
            x[2] for x in data.dataset.data_to_iterate
        ]
        mask_paths = [
            x[3] for x in data.dataset.data_to_iterate
        ]

        def image_transform(image):
            in_std = np.array(
                data.dataset.transform_std
            ).reshape(-1, 1, 1)
            in_mean = np.array(
                data.dataset.transform_mean
            ).reshape(-1, 1, 1)
            image = data.dataset.transform_img(image)
            return np.clip(
                (image.numpy() * in_std + in_mean) * 255, 0, 255
            ).astype(np.uint8)

        def mask_transform(mask):
            return data.dataset.transform_mask(mask).numpy()

        plot_segmentation_images(
            './output',
            image_paths,
            segmentations,
            scores,
            mask_paths,
            image_transform=image_transform,
            mask_transform=mask_transform,
        )

# Image handling classes.
class PatchMaker:
    def __init__(self, patchsize, top_k=0, stride=None):
        self.patchsize = patchsize
        self.stride = stride
        self.top_k = top_k

    def patchify(self, features, return_spatial_info=False):
        """Convert a tensor into a tensor of respective patches.
        Args:
            x: [torch.Tensor, bs x c x w x h]
        Returns:
            x: [torch.Tensor, bs * w//stride * h//stride, c, patchsize,
            patchsize]
        """
        padding = int((self.patchsize - 1) / 2)
        unfolder = torch.nn.Unfold(
            kernel_size=self.patchsize, stride=self.stride, padding=padding, dilation=1
        )
        unfolded_features = unfolder(features)
        number_of_total_patches = []
        for s in features.shape[-2:]:
            n_patches = (
                s + 2 * padding - 1 * (self.patchsize - 1) - 1
            ) / self.stride + 1
            number_of_total_patches.append(int(n_patches))
        unfolded_features = unfolded_features.reshape(
            *features.shape[:2], self.patchsize, self.patchsize, -1
        )
        unfolded_features = unfolded_features.permute(0, 4, 1, 2, 3)

        if return_spatial_info:
            return unfolded_features, number_of_total_patches
        return unfolded_features

    def unpatch_scores(self, x, batchsize):
        return x.reshape(batchsize, -1, *x.shape[1:])

    def score(self, x):
        was_numpy = False
        if isinstance(x, np.ndarray):
            was_numpy = True
            x = torch.from_numpy(x)
        while x.ndim > 2:
            x = torch.max(x, dim=-1).values
        if x.ndim == 2:
            if self.top_k > 1:
                x = torch.topk(x, self.top_k, dim=1).values.mean(1)
            else:
                x = torch.max(x, dim=1).values
        if was_numpy:
            return x.numpy()
        return x