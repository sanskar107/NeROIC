import os, sys
import numpy as np
import imageio
import json
import random
import time
from pytorch_lightning.utilities.distributed import rank_zero_only
from tqdm import tqdm, trange
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data
import torch.optim.lr_scheduler

import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning import loggers as pl_loggers

from opt import config_parser

from dataset.llff import LLFFDataset
from models.neroic_renderer import NeROICRenderer
import models.network.neroic as neroic

from utils.utils import *
from utils import exposure_helper
import models.sh_functions as sh
import pickle
# import OpenEXR 

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
np.random.seed(0)
DEBUG = False

class NeRFSystem(pl.LightningModule):
    def __init__(self, args):
        super().__init__()
        args.N_rand = 30000000000
        self.args = args

        if args.model == 'NeROIC':
            self.renderer = NeROICRenderer(args)
        else:
            raise ValueError("Unsupported model.")

        self.basedir = args.basedir
        self.expname = args.expname

        self.render_kwargs_test = {
            'perturb' : args.perturb,
            'N_importance' : args.N_importance,
            'N_samples' : args.N_samples,
            'use_viewdirs' : args.use_viewdirs,
            'raw_noise_std' : args.raw_noise_std,
        }

        self.render_kwargs_test['lindisp'] = args.lindisp
        self.render_kwargs_test['perturb'] = False
        self.render_kwargs_test['N_samples'] = self.render_kwargs_test['N_samples']*4
        self.render_kwargs_test['raw_noise_std'] = 0.

        self.light_param = []


    def forward(self, pixel_coords, pose, img_id): # Rendering 
        return self.renderer(pixel_coords=pixel_coords, test_pose=pose, img_id=img_id, 
                                chunk=self.args.chunk, **self.render_kwargs_train)

    def test_step(self, batch, batch_idx):
        img_id = batch['img_id'][0]#+10
        gt_imgs = batch['gt_color'][0]
        gt_masks = batch['gt_mask'][0]

        bkgd = torch.from_numpy(np.array([1,1,1])).type_as(gt_imgs)   
        gt_imgs = gt_imgs*gt_masks[...,None] + bkgd*(~gt_masks[...,None])
        
        hwf = batch['poses'][0][:3,-1]
        pose = self.renderer.get_pose(img_id, hwf)
        
        ret_dict = self.renderer.batch_render_test(pose, self.args.chunk//4, self.render_kwargs_test,
                                                    img_id=img_id, light_param=None)

        rgbs = torch.from_numpy(ret_dict['static_rgb_map'][0]).type_as(gt_imgs)
        rgbs_masked = rgbs*gt_masks[...,None] + bkgd*(~gt_masks[...,None])

        img = to8b(rgbs.clamp(0, 1).cpu().numpy()) # (H, W, 3)
        img_gt = to8b(gt_imgs.cpu().numpy()) # (H, W, 3)
        imageio.imwrite(os.path.join(self.logger.save_dir, self.args.expname, "%d.png"%batch_idx), img)
        imageio.imwrite(os.path.join(self.logger.save_dir, self.args.expname, "%d_gt.png"%batch_idx), img_gt)

        rgbs = torch.from_numpy(ret_dict['rgb_map'][0]).type_as(gt_imgs)
        img = to8b(rgbs.clamp(0, 1).cpu().numpy()) # (H, W, 3)
        imageio.imwrite(os.path.join(self.logger.save_dir, self.args.expname, "%d_with_transient.png"%batch_idx), img)

        lin_rgb = torch.from_numpy(ret_dict['hdr_rgb_map'][0]).type_as(gt_imgs)
        # lin_rgb = safe_pow(lin_rgb, 2.4)
        np.save(os.path.join(self.logger.save_dir, self.args.expname, f"pr_image_{str(batch_idx).zfill(4)}.npy"), lin_rgb.cpu().numpy())


        return

    def test_epoch_end(self, outputs):
        pass

    def setup(self, stage):
        self.args.split = self.args.test_split
        if self.args.dataset_type == 'llff':
            self.args.split = "val"
            self.test_dataset = LLFFDataset(self.args, recenter=True, bd_factor=0.75, path_zflat=False)        
        else:
            raise ValueError('Unknown dataset type: %s'%self.args.dataset_type)

        self.bds_dict = {
            'near' : self.test_dataset.near,
            'far' : self.test_dataset.far,
            'bbox': self.test_dataset.bbox,
        }
        self.render_kwargs_test.update(self.bds_dict)
        self.renderer.init_cam_pose(self.test_dataset.get_all_poses(), self.test_dataset.cx, self.test_dataset.cy)

    def test_dataloader(self):
        return torch.utils.data.DataLoader(self.test_dataset, shuffle=False, num_workers=4, batch_size=1, pin_memory=True)

def train():

    parser = config_parser()
    args = parser.parse_args()

    args.verbose = True
    args.have_mask = True # enforce bg/fg mask
    args.mask_ratio = 100000
    args.debug_green_bkgd = False

    logger = pl_loggers.TensorBoardLogger(
        save_dir="results_bmvs/nvs",
        name=args.expname
    )

    nerf_sys = NeRFSystem.load_from_checkpoint(checkpoint_path=args.ft_path, map_location=None, **{'args': args}, strict=False)

    trainer = Trainer(gpus=1, logger=logger)
    trainer.test(nerf_sys)


if __name__=='__main__':
    train()

