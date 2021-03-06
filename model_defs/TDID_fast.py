import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.models as models

import cv2
import numpy as np
import sys
import time

from instance_detection.utils.timer import Timer
from rpn_msr.proposal_layer import proposal_layer as proposal_layer_py
from rpn_msr.anchor_target_layer import anchor_target_layer as anchor_target_layer_py

import network
from network import Conv2d, FC


class TDID(nn.Module):
    groups=512

    def __init__(self, cfg):
        super(TDID, self).__init__()
        self.cfg = cfg
        self.anchor_scales = cfg.ANCHOR_SCALES

        self.features,self._feat_stride,self.num_feature_channels = \
                                    self.get_feature_net(cfg.FEATURE_NET_NAME)

        self.groups = self.num_feature_channels
        self.conv1 = self.get_conv1(cfg)
        self.cc_conv = Conv2d(cfg.NUM_TARGETS*self.num_feature_channels,
                              self.num_feature_channels, 3, 
                              relu=True, same_padding=True)
        self.diff_conv = Conv2d(cfg.NUM_TARGETS*self.num_feature_channels,
                                self.num_feature_channels, 3, 
                                relu=True, same_padding=True)
        self.score_conv = Conv2d(512, len(self.anchor_scales) * 3 * 2, 1, relu=False, same_padding=False)
        self.bbox_conv = Conv2d(512, len(self.anchor_scales) * 3 * 4, 1, relu=False, same_padding=False)

        # loss
        self.roi_cross_entropy = None
        self.cross_entropy = None
        self.loss_box = None

        self.timer = Timer()
        self.time_info = {'img_features':0}
        self.time = 0

    @property
    def loss(self):
        #return self.roi_cross_entropy + self.cross_entropy + self.loss_box * 10
        return self.cross_entropy + self.loss_box * 10
        #return self.roi_cross_entropy

    def forward(self, target_data, im_data, gt_boxes=None, features_given=False, im_info=None, return_timing_info=False):


        #self.timer.tic()
        self.time = time.clock() 
        #get image features 
        if features_given:
            img_features = im_data
            target_features = target_data
        else:
            img_features = self.features(im_data)
            target_features = self.features(target_data)

        #featrues timing end
        padding = (max(0,int(target_features.size()[2]/2)), 
                         max(0,int(target_features.size()[3]/2)))
        ccs = []
        diffs = []

        sample_img = img_features
        diff = []
        cc = []
        sample_target1 = target_features[0,:,:,:].unsqueeze(0)
        sample_target2 = target_features[1,:,:,:].unsqueeze(0)

        sample_target1 = sample_target1.permute((1,0,2,3)) 
        sample_target2 = sample_target2.permute((1,0,2,3)) 

        tf_pooled1 = F.max_pool2d(sample_target1,(sample_target1.size()[2],
                                                   sample_target1.size()[3]))
        tf_pooled2 = F.max_pool2d(sample_target2,(sample_target2.size()[2],
                                                   sample_target2.size()[3]))

        diff.append(sample_img - tf_pooled1.permute(1,0,2,3).expand_as(sample_img))
        diff.append(sample_img - tf_pooled2.permute(1,0,2,3).expand_as(sample_img))
        cc.append(F.conv2d(sample_img,tf_pooled1,groups=self.groups))
        cc.append(F.conv2d(sample_img,tf_pooled2,groups=self.groups))

        #pooll/diff/corr timing end

        cc = torch.cat(cc,1)
        diffs = torch.cat(diff,1)

        cc = self.cc_conv(cc)
        diffs = self.diff_conv(diffs)
        cc = torch.cat([cc,diffs],1) 
        rpn_conv1 = self.conv1(cc)

        #rpnconv timing end

 
        # rpn score
        rpn_cls_score = self.score_conv(rpn_conv1)
        rpn_cls_score_reshape = self.reshape_layer(rpn_cls_score, 2)
        rpn_cls_prob = F.softmax(rpn_cls_score_reshape)
        rpn_cls_prob_reshape = self.reshape_layer(rpn_cls_prob, len(self.anchor_scales)*3*2)

        # rpn boxes
        rpn_bbox_pred = self.bbox_conv(rpn_conv1)


        #score/reg timgin end

        # proposal layer
        rois,scores, anchor_inds, labels = self.proposal_layer(rpn_cls_prob_reshape, 
                                                               rpn_bbox_pred,
                                                               im_info,
                                                               self.cfg,
                                                               self._feat_stride, 
                                                               self.anchor_scales,
                                                               gt_boxes)
    
        #rois = network.np_to_variable(np.zeros((1,300,4)),is_cuda=False)
        #scores = network.np_to_variable(np.zeros((1,300,1)),is_cuda=False)
        #anchor_inds =network.np_to_variable(np.zeros((1,300,1)),is_cuda=False,dtype=torch.cuda.LongTensor) 
        #labels = network.np_to_variable(np.zeros((1,300)),is_cuda=False,dtype=torch.cuda.LongTensor) 
        #rois = np.zeros((1,300,4))
        #scores = np.zeros((1,300,1))
        #anchor_inds =np.zeros((1,300,1))
        #labels = np.zeros((1,300)) 
        #self.time_info['img_features'] = self.timer.toc(average=False)
        self.time_info['img_features'] = time.clock() - self.time
        #prop timing end

        #return target_features, features, rois, scores
        if return_timing_info:
            return scores.data.cpu().numpy(), rois.data.cpu().numpy(), self.time_info
        else:
            return scores, rois



    def build_loss(self, rpn_cls_score_reshape, rpn_bbox_pred, rpn_data):
        # classification loss
        rpn_cls_score = rpn_cls_score_reshape.permute(0, 2, 3, 1).contiguous().view(-1, 2)

        rpn_label = rpn_data[0].view(-1)
        rpn_keep = Variable(rpn_label.data.ne(-1).nonzero().squeeze()).cuda()
        rpn_cls_score = torch.index_select(rpn_cls_score, 0, rpn_keep)
        rpn_label = torch.index_select(rpn_label, 0, rpn_keep)

        fg_cnt = torch.sum(rpn_label.data.ne(0))

        # box loss
        rpn_bbox_targets = rpn_data[1]
        rpn_bbox_inside_weights = rpn_data[2]
        rpn_bbox_outside_weights = rpn_data[3]
        rpn_bbox_targets = torch.mul(rpn_bbox_targets, rpn_bbox_inside_weights)
        rpn_bbox_pred = torch.mul(rpn_bbox_pred, rpn_bbox_inside_weights)

        rpn_cross_entropy = F.cross_entropy(rpn_cls_score, rpn_label, size_average=False)
        rpn_loss_box = F.smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets, size_average=False) / (fg_cnt + 1e-4)
        return rpn_cross_entropy, rpn_loss_box


    def build_roi_loss(self, rpn_cls_score_reshape, rpn_cls_prob_reshape, scores, anchor_inds, labels):

        batch_size = rpn_cls_score_reshape.size()[0]
        rpn_cls_score = rpn_cls_score_reshape.permute(0, 2, 3, 1)#.contiguous().view(-1, 2)
        bg_scores = torch.index_select(rpn_cls_score,3,network.np_to_variable(np.arange(0,9),is_cuda=True, dtype=torch.LongTensor))
        fg_scores = torch.index_select(rpn_cls_score,3,network.np_to_variable(np.arange(9,18),is_cuda=True, dtype=torch.LongTensor))
        bg_scores = bg_scores.contiguous().view(-1,1)
        fg_scores = fg_scores.contiguous().view(-1,1)

        rpn_cls_score = torch.cat([bg_scores, fg_scores],1)

        rpn_cls_score = torch.index_select(rpn_cls_score, 0, anchor_inds.view(-1))
        labels = labels.view(-1)

        roi_cross_entropy = F.cross_entropy(rpn_cls_score, labels, size_average=False)

        return roi_cross_entropy


    @staticmethod
    def reshape_layer(x, d):
        input_shape = x.size()
        # b c w h
        x = x.view(
            input_shape[0],
            int(d),
            int(float(input_shape[1] * input_shape[2]) / float(d)),
            input_shape[3]
        )
        return x

    @staticmethod
    def select_to_match_dimensions(a,b):
        if a.size()[2] > b.size()[2]:
            a = torch.index_select(a, 2, 
                                  network.np_to_variable(np.arange(0,
                                        b.size()[2]).astype(np.int32),
                                         is_cuda=True,dtype=torch.LongTensor))
        if a.size()[3] > b.size()[3]:
            a = torch.index_select(a, 3, 
                                  network.np_to_variable(np.arange(0,
                                    b.size()[3]).astype(np.int32),
                                          is_cuda=True,dtype=torch.LongTensor))
        return a 


    @staticmethod
    def proposal_layer(rpn_cls_prob_reshape, rpn_bbox_pred, im_info, cfg, _feat_stride, anchor_scales, gt_boxes=None):
        
        #convert to  numpy
        rpn_cls_prob_reshape = rpn_cls_prob_reshape.data.cpu().numpy()
        rpn_bbox_pred = rpn_bbox_pred.data.cpu().numpy()

        rois, scores, anchor_inds, labels = proposal_layer_py(rpn_cls_prob_reshape,
                                                               rpn_bbox_pred,
                                                      im_info, cfg, 
                                                      _feat_stride=_feat_stride,
                                                      anchor_scales=anchor_scales,
                                                      gt_boxes=gt_boxes)
        rois = network.np_to_variable(rois, is_cuda=True)
        anchor_inds = network.np_to_variable(anchor_inds, is_cuda=True,
                                                 dtype=torch.LongTensor)
        labels = network.np_to_variable(labels, is_cuda=True,
                                             dtype=torch.LongTensor)
        #just get fg scores, make bg scores 0 
        scores = network.np_to_variable(scores, is_cuda=True)
        return rois, scores, anchor_inds, labels


    @staticmethod
    def anchor_target_layer(rpn_cls_score, gt_boxes, im_info,
                            cfg, _feat_stride, anchor_scales):
        """
        rpn_cls_score: for pytorch (1, Ax2, H, W) bg/fg scores of previous conv layer
        gt_boxes: (G, 5) vstack of [x1, y1, x2, y2, class]
        gt_ishard: (G, 1), 1 or 0 indicates difficult or not
        dontcare_areas: (D, 4), some areas may contains small objs but no labelling. D may be 0
        im_info: a list of [image_height, image_width, scale_ratios]
        _feat_stride: the downsampling ratio of feature map to the original input image
        anchor_scales: the scales to the basic_anchor (basic anchor is [16, 16])
        ----------
        Returns
        ----------
        rpn_labels : (1, 1, HxA, W), for each anchor, 0 denotes bg, 1 fg, -1 dontcare
        rpn_bbox_targets: (1, 4xA, H, W), distances of the anchors to the gt_boxes(may contains some transform)
                        that are the regression objectives
        rpn_bbox_inside_weights: (1, 4xA, H, W) weights of each boxes, mainly accepts hyper param in cfg
        rpn_bbox_outside_weights: (1, 4xA, H, W) used to balance the fg/bg,
        beacuse the numbers of bgs and fgs mays significiantly different
        """
        rpn_cls_score = rpn_cls_score.data.cpu().numpy()
        rpn_labels, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights = \
            anchor_target_layer_py(rpn_cls_score, gt_boxes, im_info,
                                   cfg, _feat_stride, anchor_scales)

        rpn_labels = network.np_to_variable(rpn_labels, is_cuda=True, dtype=torch.LongTensor)
        rpn_bbox_targets = network.np_to_variable(rpn_bbox_targets, is_cuda=True)
        rpn_bbox_inside_weights = network.np_to_variable(rpn_bbox_inside_weights, is_cuda=True)
        rpn_bbox_outside_weights = network.np_to_variable(rpn_bbox_outside_weights, is_cuda=True)

        return rpn_labels, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights

    def get_features(self, im_data):
        im_data = network.np_to_variable(im_data, is_cuda=True)
        im_data = im_data.permute(0, 3, 1, 2)
        features = self.features(im_data)

        return features


    @staticmethod
    def get_feature_net(net_name):
        if net_name == 'vgg16_bn':
            fnet = models.vgg16_bn(pretrained=False)
            return torch.nn.Sequential(*list(fnet.features.children())[:-1]), 16, 512
        elif net_name == 'squeezenet1_1':
            fnet = models.squeezenet1_1(pretrained=False)
            return torch.nn.Sequential(*list(fnet.features.children())[:-1]), 16, 512 
        elif net_name == 'resnet101':
            fnet = models.resnet101(pretrained=False)
            return torch.nn.Sequential(*list(fnet.children())[:-2]), 32, 2048 
        else:
            print 'feature net type not supported!'
            sys.exit() 
   
    def get_conv1(self,cfg):
        if cfg.USE_IMG_FEATS and cfg.USE_DIFF_FEATS: 
            if cfg.USE_CC_FEATS:
                return Conv2d(3*self.num_feature_channels,
                                512, 3, relu=False, same_padding=True)
            else:
                return Conv2d(2*self.num_feature_channels,
                                512, 3, relu=False, same_padding=True)
        elif cfg.USE_IMG_FEATS:
            if cfg.USE_CC_FEATS:
                return Conv2d(2*self.num_feature_channels,
                                512, 3, relu=False, same_padding=True)
            else:
                return Conv2d(self.num_feature_channels,
                                512, 3, relu=False, same_padding=True)
        elif cfg.USE_DIFF_FEATS:
            if cfg.USE_CC_FEATS:
                return Conv2d(2*self.num_feature_channels,
                                512, 3, relu=False, same_padding=True)
            else:
                return Conv2d(self.num_feature_channels,
                                512, 3, relu=False, same_padding=True)
        else:
            return Conv2d(self.num_feature_channels,
                            512, 3, relu=False, same_padding=True)
         
        Conv2d(3*self.num_feature_channels,
                            512, 3, relu=False, same_padding=True)
