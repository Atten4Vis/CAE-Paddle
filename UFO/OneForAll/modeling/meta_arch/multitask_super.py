"""Build MultiTaskBatchFuse
"""
# Copyright (c) 2022  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
import logging

import paddle
from paddle import nn

from modeling.losses import triplet_loss, cross_entropy_loss, log_accuracy

logger = logging.getLogger(__name__)

def sample_configs(choices, seed=42):
    """sample_configs
    """
    random.seed(seed)
    config = {}
    dimensions = ['mlp_ratio', 'num_heads']
    depth = random.choice(choices['depth'])
    for dimension in dimensions:
        config[dimension] = [random.choice(choices[dimension]) for _ in range(depth)]

    config['embed_dim'] = [random.choice(choices['embed_dim'])]*depth

    config['layer_num'] = depth
    config['img_size'] = random.choice(choices['img_size'])

    largest_depth = max(choices['depth'])
    config['mlp_ratio'] = config['mlp_ratio'] + [config['mlp_ratio'][-1]] * (largest_depth - depth)
    config['num_heads'] = config['num_heads'] + [config['num_heads'][-1]] * (largest_depth - depth)
    config['embed_dim'] = config['embed_dim'] + [config['embed_dim'][-1]] * (largest_depth - depth)
    return config


class MultiTaskBatchFuse(nn.Layer):
    """
    Baseline architecture. Any models that contains the following two components:
    1. Per-image feature extraction (aka backbone)
    2. Per-image feature aggregation and loss computation
    """

    def __init__(
            self,
            backbone,
            heads,
            pixel_mean,
            pixel_std,
            task_loss_kwargs=None,
            task2head_mapping=None,
            choices={
            'num_heads': [10, 11, 12], 'mlp_ratio': [3.0, 3.5, 4.0], \
                'embed_dim': [768], 'depth': [10, 11, 12], 'img_size': [224]
                }
    ):
        """
        NOTE: this interface is experimental.

        Args:
            backbone:
            heads:
            pixel_mean:
            pixel_std:
        """
        super().__init__()
        # backbone
        self.backbone = backbone

        # head
        # use nn.LayerDict to ensure head modules are properly registered
        self.heads = nn.LayerDict(heads)

        if task2head_mapping is None:
            task2head_mapping = {}
            for key in self.heads:
                task2head_mapping[key] = key
        self.task2head_mapping = task2head_mapping

        self.task_loss_kwargs = task_loss_kwargs

        self.register_buffer('pixel_mean', paddle.to_tensor(list(pixel_mean)).reshape((1, -1, 1, 1)), False)
        self.register_buffer('pixel_std', paddle.to_tensor(list(pixel_std)).reshape((1, -1, 1, 1)), False)

        self.choices = choices
        self.largest_choices = {key:[max(value)] for key, value in choices.items()}
        self.smallest_choices = {key:[min(value)] for key, value in choices.items()}
        logger.info(self.choices)
        logger.info(self.largest_choices)
        logger.info(self.smallest_choices)
        self.STEP=0
        self.forced_config = None
        self.subnet_mode = None

    @property
    def device(self):
        """
        Get device information
        """
        return self.pixel_mean.device

    def forward(self, task_batched_inputs):
        """
        NOTE: this forward function only supports `self.training is False`
        """
        #sampling a config
        if self.forced_config is not None:
            self.config = self.forced_config
        else:
            if self.training:
                self.config = sample_configs(self.choices, seed = self.STEP)
            else:
                if hasattr(self, "subnet_mode"):
                    if self.subnet_mode == "largest":
                        # logger.info("sampling the {} subnet in evaluation ".format(self.subnet_mode))
                        self.config = sample_configs(self.largest_choices, seed = self.STEP)
                    elif self.subnet_mode == "smallest":
                        # logger.info("sampling the {} subnet in evaluation ".format(self.subnet_mode))
                        self.config = sample_configs(self.smallest_choices, seed = self.STEP)
                    else:
                        self.config = sample_configs(self.smallest_choices, seed = self.STEP)
                else:
                    self.config = sample_configs(self.largest_choices, seed = self.STEP)
            
        if hasattr(self.backbone, "_layers"):
            self.backbone._layers.set_sample_config(config=self.config)
        else:
            self.backbone.set_sample_config(config=self.config)
        # fuse batch
        img_list = []
        task_data_idx = {}
        start = 0
        for task_name, batched_inputs in task_batched_inputs.items():
            images = self.preprocess_image(batched_inputs)
            img_list.append(images)

            end = start + images.shape[0]
            task_data_idx[task_name] = (start, end)
            start = end
        all_imgs = paddle.concat(img_list, axis=0)
        all_features = self.backbone(all_imgs)

        losses = {}
        outputs = {}
        for task_name, batched_inputs in task_batched_inputs.items():
            start, end = task_data_idx[task_name]
            features = all_features[start:end, ...]

            if self.training:
                assert "targets" in batched_inputs, "Person ID annotation are missing in training!"
                targets = batched_inputs["targets"]

                # PreciseBN flag, When do preciseBN on different dataset, the number of classes in new dataset
                # may be larger than that in the original dataset, so the circle/arcface will
                # throw an error. We just set all the targets to 0 to avoid this problem.
                if targets.sum() < 0: targets.zero_()

                task_outputs = self.heads[self.task2head_mapping[task_name]](features, targets)
                task_losses = self.losses(task_name, task_outputs, targets)
                losses.update(**task_losses)
            else:
                task_outputs = self.heads[self.task2head_mapping[task_name]](features)
                outputs[task_name] = task_outputs
        
        self.STEP += 1
        
        if self.training:
            return losses
        else:
            return outputs

    def preprocess_image(self, batched_inputs):
        """
        Normalize and batch the input images.
        """
        if isinstance(batched_inputs, dict):
            images = batched_inputs['images']
        elif isinstance(batched_inputs, paddle.Tensor):
            images = batched_inputs
        else:
            raise TypeError("batched_inputs must be dict or torch.Tensor, but get {}".format(type(batched_inputs)))

        # images.sub_(self.pixel_mean).div_(self.pixel_std)
        return images

    def losses(self, task_name, outputs, gt_labels):
        """
        Compute loss from modeling's outputs, the loss function input arguments
        must be the same as the outputs of the model forwarding.
        """
        # model predictions
        # fmt: off
        pred_features     = outputs['features']
        # fmt: on

        loss_dict = {}
        loss_kwargs = self.task_loss_kwargs[task_name]
        loss_names = loss_kwargs['loss_names']

        loss_exist = 1.0
        if 'CrossEntropyLoss' in loss_names:
            pred_class_logits = outputs['pred_class_logits'].detach()
            cls_outputs = outputs['cls_outputs']
            # Log prediction accuracy
            # acc = log_accuracy(pred_class_logits, gt_labels)

            ce_kwargs = loss_kwargs.get('ce', {})
            ce_prob = ce_kwargs.get('prob', 1.0)
            if random.random() < ce_prob:
                loss_exist = 1.0
            else:
                loss_exist = 0.0
            loss_dict[task_name + '_loss_cls'] = cross_entropy_loss(
                cls_outputs,
                gt_labels,
                ce_kwargs.get('eps', 0.0),
                ce_kwargs.get('alpha', 0.2)
            ) * ce_kwargs.get('scale', 1.0) * loss_exist

        if 'TripletLoss' in loss_names:
            tri_kwargs = loss_kwargs.get('tri', {})
            loss_dict[task_name + '_loss_triplet'] = triplet_loss(
                pred_features,
                gt_labels,
                tri_kwargs.get('margin', 0.3),
                tri_kwargs.get('norm_feat', False),
                tri_kwargs.get('hard_mining', False)
            ) * tri_kwargs.get('scale', 1.0) * loss_exist

        # if 'CircleLoss' in loss_names:
        #     circle_kwargs = loss_kwargs.get('circle', {})
        #     loss_dict[task_name + '_loss_circle'] = pairwise_circleloss(
        #         pred_features,
        #         gt_labels,
        #         circle_kwargs.get('margin', 0.25),
        #         circle_kwargs.get('gamma', 128)
        #     ) * circle_kwargs.get('scale', 1.0)

        # if 'Cosface' in loss_names:
        #     cosface_kwargs = loss_kwargs.get('cosface', {})
        #     loss_dict[task_name + '_loss_cosface'] = pairwise_cosface(
        #         pred_features,
        #         gt_labels,
        #         cosface_kwargs.get('margin', 0.25),
        #         cosface_kwargs.get('gamma', 128),
        #     ) * cosface_kwargs.get('scale', 1.0)

        return loss_dict