from gsplat.project_gaussians_2d import project_gaussians_2d
from gsplat.rasterize_sum import rasterize_gaussians_sum
from utils import *
import torch
import torch.nn as nn
import numpy as np
import math
from optimizer import Adan

class GaussianBasis(nn.Module):
    def __init__(self, loss_type="L2", **kwargs):
        super().__init__()
        self.loss_type = loss_type
        self.init_num_points = kwargs["num_points"]
        self.num_comps = kwargs["num_comps"]
        self.H, self.W = kwargs["H"], kwargs["W"]
        self.BLOCK_W, self.BLOCK_H = kwargs["BLOCK_W"], kwargs["BLOCK_H"]
        self.tile_bounds = (
            (self.W + self.BLOCK_W - 1) // self.BLOCK_W,
            (self.H + self.BLOCK_H - 1) // self.BLOCK_H,
            1,
        )
        self.device = kwargs["device"]

        self._xyz = nn.Parameter(torch.atanh(2 * (torch.rand(self.init_num_points, 2) - 0.5)))
        self._cholesky = nn.Parameter(torch.rand(self.init_num_points, 3))
        self.register_buffer('_opacity', torch.ones((self.init_num_points, 1)))
        self._features_dc = nn.Parameter(torch.rand(self.num_comps, self.init_num_points, 3))
        self._colors = nn.Parameter(torch.empty(self.init_num_points, 3))

        self.register_buffer('shift_factor', torch.tensor(0.0, device=self.device))
        self.register_buffer('scale_factor', torch.tensor(1.0, device=self.device))
        self.register_buffer('image_mean', torch.zeros(self.H * self.W, device=self.device))

        self.register_buffer('background', torch.ones(3))
        self.opacity_activation = torch.sigmoid
        self.rgb_activation = torch.sigmoid
        self.register_buffer('cholesky_bound', torch.tensor([0.5, 0, 0.5]).view(1, 3))

        self.opt_type = kwargs["opt_type"]
        self.lr = kwargs["lr"]

    @property
    def get_xyz(self):
        return torch.tanh(self._xyz)

    @property
    def get_colors(self):
        return self._colors

    @property
    def get_features(self):
        return self._features_dc
    
    @property
    def get_opacity(self):
        return self._opacity

    @property
    def get_cholesky_elements(self):
        return self._cholesky+self.cholesky_bound

    def _forward_colors(self):
        self.xys, depths, self.radii, conics, num_tiles_hit = project_gaussians_2d(
            self.get_xyz, self.get_cholesky_elements, 
            self.H, self.W, self.tile_bounds
        )
        out_img = rasterize_gaussians_sum(
            self.xys, depths, self.radii, conics, num_tiles_hit,
            self.get_colors, self._opacity, self.H, self.W, self.BLOCK_H, self.BLOCK_W,
            background=self.background, return_alpha=False
        )
        out_img *= self.scale_factor
        out_img += self.shift_factor
        out_img = out_img.permute(2, 0, 1).contiguous()
        return out_img

    # def _forward_featrues_dc(self):
    #     self.xys, depths, self.radii, conics, num_tiles_hit = project_gaussians_2d(
    #         self.get_xyz, self.get_cholesky_elements, 
    #         self.H, self.W, self.tile_bounds
    #     )

    #     num_streams = 1
    #     streams = [torch.cuda.Stream() for _ in range(num_streams)]
    #     chunk_size = (self.num_comps + num_streams - 1) // num_streams
        
    #     output = torch.zeros((self.num_comps, 3, self.H, self.W), device=self.device)
    #     for stream_idx, stream in enumerate(streams):
    #         start_idx = stream_idx * chunk_size
    #         end_idx = min(start_idx + chunk_size, self.num_comps)
                
    #         with torch.cuda.stream(stream):
    #             for i in range(start_idx, end_idx):
    #                 out_img = rasterize_gaussians_sum(
    #                     self.xys, depths, self.radii, conics, num_tiles_hit,
    #                     self.get_features[i], self._opacity, self.H, self.W, 
    #                     self.BLOCK_H, self.BLOCK_W,
    #                     background=self.background, return_alpha=False
    #                 )
    #                 out_img = torch.clamp(out_img, 0, 1)
    #                 output[i] = out_img.permute(2, 0, 1).contiguous()

    #     torch.cuda.synchronize()
    #     return output

    def _forward_featrues_dc(self):
        self.xys, depths, self.radii, conics, num_tiles_hit = project_gaussians_2d(
            self.get_xyz, self.get_cholesky_elements, 
            self.H, self.W, self.tile_bounds
        )
        comps = []
        for i in range(self.num_comps):
            out_img = rasterize_gaussians_sum(
                self.xys, depths, self.radii, conics, num_tiles_hit,
                self.get_features[i], self._opacity, self.H, self.W, self.BLOCK_H, self.BLOCK_W,
                background=self.background, return_alpha=False
            )
            # out_img = torch.clamp(out_img, 0, 1)
            out_img = out_img.permute(2, 0, 1).contiguous()
            comps.append(out_img)
            
        out_img = torch.stack(comps, dim=0)
        return out_img

    def forward(self, render_colors=False):
        if render_colors:
            return self._forward_colors()
        else:
            return self._forward_featrues_dc()

    def train_iter(self, gt_image):
        image = self.forward()
        loss = loss_fn(image, gt_image, self.loss_type, lambda_value=0.7)
        loss.backward()
        with torch.no_grad():
            mse_loss = F.mse_loss(image, gt_image)
            psnr = 10 * math.log10(1.0 / mse_loss.item())
        self.optimizer.step()
        self.optimizer.zero_grad(set_to_none = True)

        self.scheduler.step()
        return loss, psnr

    def optimize_iter(self, gt_image):
        out = self.forward(render_colors=True)
        image = out.reshape(3, -1) + self.image_mean
        image = image.reshape(3, self.H, self.W)
        loss = loss_fn(image, gt_image, self.loss_type, lambda_value=0.7)
        loss.backward()
        with torch.no_grad():
            mse_loss = F.mse_loss(image, gt_image)
            psnr = 10 * math.log10(1.0 / mse_loss.item())
        self.optimizer.step()
        self.optimizer.zero_grad(set_to_none=True)
        self.scheduler.step()
        return loss, psnr


    def scheduler_init(self, optimize_phase=False):
        if not optimize_phase:
            params = [p for n, p in self.named_parameters() if n != '_colors']
            self._colors.requires_grad_(False)
            self._features_dc.requires_grad_(True)
        else:
            params = [p for n, p in self.named_parameters() if n != '_features_dc']
            self._colors.requires_grad_(True)
            self._features_dc.requires_grad_(False)

        if self.opt_type == "adam":
            self.optimizer = torch.optim.Adam(params, lr=self.lr)
        else:
            self.optimizer = Adan(params, lr=self.lr)
        self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=20000, gamma=0.5)