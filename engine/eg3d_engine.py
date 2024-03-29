import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from hydra.utils import instantiate
from omegaconf import OmegaConf
import logging
from typing import List
from visdom import Visdom
from data.utils import trivial_collate
import os 
import pickle
from PIL import Image
from .basic_engine import NerfEngine as BasicEngine
from .utils import Stats, visualize_nerf_gan_outputs
from gan_utils.losses import lpips
from gan_utils.losses.id.id_loss import IDLoss

log = logging.getLogger(__name__)

class NerfEngine(BasicEngine):
    def __init__(self, cfg):
        self.cfg = cfg
        
        # specifying device
        if not torch.cuda.is_available() and cfg.device == 'cuda':
            log.info('Specifying expreriment on GPU while GPU is not available')
            raise Exception
        
        self.device = cfg.device
        log.info('Specify {} as the device.'.format(self.device))

    # gan function
    def requires_grad(self, 
                      model, 
                      flag=True):
        for p in model.parameters():
            p.requires_grad = flag

    def train(self, ):
        for epoch in range(self.start_epoch, self.cfg.optimizer.max_epochs):
            self.curr_epoch = epoch
            self.train_epoch()
            if self.sched_gen is not None:
                self.sched_gen.step()
            if self.sched_dis is not None:
                self.sched_dis.step()
            if epoch % self.cfg.validation_epoch_interval == 0 and epoch > 0: 
                self.val_epoch()
            if epoch % self.cfg.checkpoint_epoch_interval == 0 and epoch > 0:
                self.save_checkpoint()
                
    def train_epoch(self, ):
        self.gen.train()
        self.dis.train()
        self.stats.new_epoch()
        
        
        for num_dataset, dataloader in enumerate(self.train_dataloaders):
            # choose to fetch data via Dataset rather than Dataloader, because of the following operations.
            dataset = dataloader.dataset
            bg_image = dataset.meta['bg_image'].permute(2,0,1).unsqueeze(0).to(self.device)
            for iteration, batch in enumerate(dataloader):
                
                self.optim_gen.zero_grad()
                self.optim_dis.zero_grad()

                image, camera, audio, _, camera_idx = batch.values()
                
                #####################################################
                # Train discriminator
                self.requires_grad(self.gen, False)
                self.requires_grad(self.dis, True)
                
                # Run the forward pass of the model
                other_params = {
                    'camera_hash':  camera_idx if self.cfg.precache_rays else None, 
                    'camera': camera.to(self.device),
                    'ref_image': bg_image}  # arguments that original RadianceFieldRenderer in vanillar Nerf used
                nerf_out = self.gen(
                    audio = audio.to(self.device),
                    **other_params)
                
                loss_dict = {}
                gt_image = image.permute(0, 3, 1, 2).to(self.device)
                
                # Discriminator prediction on fake images
                fake_pred = F.softplus(   self.dis(nerf_out)).mean()
                real_pred = F.softplus( - self.dis(gt_image)).mean()
                d_loss = self.cfg.losses.gan * (fake_pred + real_pred)
                
                # Optimize discriminator
                d_loss.backward()
                self.optim_dis.step()
                
                # Record loss terms
                loss_dict['loss_dis'] = d_loss
                loss_dict['dis_real'] = real_pred
                loss_dict['dis_fake'] = fake_pred
                
                # Train generator 
                self.requires_grad(self.gen, True)
                self.requires_grad(self.dis, False)

                # Run the forward pass of the model
                other_params = {
                    'camera_hash':  camera_idx if self.cfg.precache_rays else None, 
                    'camera': camera.to(self.device),
                    'ref_image': bg_image}  # arguments that original RadianceFieldRenderer in vanillar Nerf used
                nerf_out = self.gen(
                    audio = audio.to(self.device),
                    **other_params)
                
                # Discriminator predictions on fake images
                fake_pred = self.dis(nerf_out)
                
                # Collect losses
                gan_loss = F.softplus(-fake_pred).mean()
                l1_loss = self.l1_func(
                    nerf_out, 
                    gt_image)
                percept_loss = self.lpips_func(
                    nerf_out, 
                    gt_image).mean()
                id_loss, _, _ = self.id_func(nerf_out, gt_image, bg_image); id_loss = torch.ones_like(id_loss) - id_loss
                g_loss = self.cfg.losses.gan * gan_loss + \
                         self.cfg.losses.l1 * l1_loss + \
                         self.cfg.losses.percept * percept_loss + \
                         self.cfg.losses.id * id_loss
                
                # Optimizer generator
                g_loss.backward()
                self.optim_gen.step()
                
                # Record loss terms
                loss_dict['loss_gen'] = g_loss
                loss_dict['gen_gan'] = gan_loss
                loss_dict['gen_l1'] = l1_loss
                loss_dict['gen_percept'] = percept_loss
                loss_dict['gen_id'] = id_loss     
                loss_dict['gen_psnr'] =   -10.0 * torch.log10( torch.mean((nerf_out - gt_image) ** 2))     
                #####################################################

                # Update stats with the current metrics.
                self.stats.update(
                    {**loss_dict},
                    stat_set= 'train')
            
                if iteration % self.cfg.stats_print_interval == 0:
                    self.stats.print(stat_set="train")
            
                # Update the visualization cache.
                if self.viz is not None:
                    self.visuals_cache.append({
                        "camera": camera.cpu(),
                        "camera_idx":camera_idx,
                        "first_frame":image.cpu().detach(),
                        "pred_frame": nerf_out.cpu().detach(),
                        "gt_frame": gt_image.cpu().detach(),
                        })

            log.info('Training done on {} datasets'.format(num_dataset))

    def videos_synthesis(self, ):
        self.curr_epoch = self.stats.epoch
        self.gen.eval()

        frame_paths = []
        for num_dataset, test_dataloader in enumerate(self.test_dataloaders):
            
            # choose to fetch data via Dataset rather than Dataloader, because of the following operations.
            dataset = test_dataloader.dataset
            bg_image = dataset.meta['bg_image'].permute(2,0,1).unsqueeze(0).to(self.device)

            for iteration, test_batch in enumerate(test_dataloader):
                
                # Unpack values
                test_image, test_camera, test_audio, _, test_camera_idx = test_batch.values()
                
                # Activate eval mode of the model (lets us do a full rendering pass).
                with torch.no_grad():

                    # Run the foward pass of the model
                    other_params = {
                        'camera_hash':  test_camera_idx if self.cfg.precache_rays else None, 
                        'camera': test_camera.to(self.device),
                        'ref_image': bg_image}  # arguments that original RadianceFieldRenderer in vanillar Nerf used
                    
                    test_nerf_out, _ = self.gen(
                        audio = test_audio.to(self.device),
                        **other_params)
                
                # Writing images
                frame = test_nerf_out["rgb_fine"].detach().cpu()
                frame_path = os.path.join(self.export_dir, f"scene_{num_dataset:01d}_frame_{iteration:05d}.png")
                log.info(f"Writing {frame_path}")
                tensor2np = lambda x: (x.detach().cpu().numpy() * 255.0).astype(np.uint8)
                if self.cfg.test.with_gt:
                    Image.fromarray(
                        np.hstack([
                            tensor2np(test_image),
                            tensor2np(frame)])
                        ).save(frame_path)
                else:
                    Image.fromarray(tensor2np(frame)).save(frame_path)
                frame_paths.append(frame_path)
                                
        # Convert the exported frames to a video
        video_path = os.path.join(os.getcwd(), "video.mp4")
        ffmpeg_bin = "ffmpeg"
        frame_regexp = os.path.join(os.getcwd(), self.export_dir,"*.png" )
        ffmcmd = (
            "%s -r %d -pattern_type glob -i '%s' -f mp4 -y -b:v 2000k -pix_fmt yuv420p %s"
            %(ffmpeg_bin, self.cfg.test.fps, frame_regexp, video_path))
        log.info('Video gnerated via {} \n {}'.format(ffmpeg_bin, ffmcmd))
        ret = os.system(ffmcmd)
        if ret != 0:
            raise RuntimeError("ffmpeg failed!")   

    def evaluate_full(self, ):
        self.val_epoch()
                
    def val_epoch(self, ):
        self.gen.eval()
        
        # Prepare evaluation metrics
        if not hasattr(self, 'lpips_func'):
            self.lpips_func = lpips.LPIPS(net='alex', version='0.1').to(self.device)
        if not hasattr(self, 'id_func'):
            self.id_func = IDLoss(device = self.device)
    
        for num_dataset, dataloader in enumerate(self.val_dataloaders):

            # choose to fetch data via Dataset rather than Dataloader, because of the following operations.
            dataset = dataloader.dataset
            bg_image = dataset.meta['bg_image'].permute(2,0,1).unsqueeze(0).to(self.device)

            for iteration, batch in enumerate(dataloader):
                idx = iteration
                
                # Unpack values
                image, camera, audio, _, camera_idx = batch.values()               
                gt_image = image.permute(0, 3, 1, 2).to(self.device)
                # Activate eval mode of the model (lets us do a full rendering pass).
                with torch.no_grad():
                    
                    # Run the forward pass of the model
                    other_params = {
                        'camera_hash':  camera_idx if self.cfg.precache_rays else None, 
                        'camera': camera.to(self.device),
                        'ref_image': bg_image}  # arguments that original RadianceFieldRenderer in vanillar Nerf used
                    val_nerf_out = self.gen(
                        audio = audio.to(self.device),
                        **other_params)

                val_metrics = {}
                val_metrics["gen_percept"] = self.lpips_func(val_nerf_out, gt_image).mean()
                id_loss, _, _ = self.id_func(val_nerf_out, gt_image, bg_image); id_loss = torch.ones_like(id_loss) - id_loss
                val_metrics["gen_id"] = id_loss
                val_metrics["gen_psnr"] = -10.0 * torch.log10( torch.mean((val_nerf_out - gt_image) ** 2))                  
                
                # Update stats with the validation metrics.  
                self.stats.update(val_metrics, stat_set="val")

            log.info('Validation done on {} datasets'.format(num_dataset + 1))
            
        self.stats.print(stat_set='val')
        
        if self.viz is not None:
            # Plot that loss curves into visdom.
            self.stats.plot_stats(
                viz = self.viz, 
                visdom_env = self.cfg.visualization.visdom_env,
                plot_file = None)
            log.info('Loss curve ploted in visdom')
            
            # Visualize the intermediate results.
            visualize_nerf_gan_outputs(
                nerf_out = {'rgb_gt': gt_image , 'rgb_pred': val_nerf_out, 'rgb_ref': bg_image}, 
                output_cache = self.visuals_cache,
                viz = self.viz, 
                visdom_env = self.cfg.visualization.visdom_env)
            log.info('Visualization saved in visdom')

    
    def build_networks(self,):
        # merge configs in cfg.render, cfg. raysampler, implicit_function
        # since they all correspond to Nerf
        log.info('Initializing nerf model..')
        OmegaConf.set_struct(self.cfg.renderer, False)
        renderer_cfg = OmegaConf.merge(
            self.cfg.renderer, 
            self.cfg.raysampler,
            )
        self.gen = instantiate(
            renderer_cfg,
            image_size = self.cfg.data.dataset.image_size
        ).to(self.device)
        components = instantiate(self.cfg.components)
        if self.cfg.train:
            self.dis = components['discriminator'].to(self.device)
            self.l1_func = nn.SmoothL1Loss().to(self.device)
            self.lpips_func = lpips.LPIPS(net='alex', version='0.1').to(self.device)
            self.id_func = IDLoss(device = self.device)
        
    
    def setup_optimizer(self,):
        log.info('Setting up optimizers..')
        optim_cfg = self.cfg.optimizer
        
        # fixing name typo
        if optim_cfg.algo == 'adam': optim_cfg.algo = 'Adam'
        if optim_cfg.algo == 'sgd': optim_cfg.algo = 'SGD'
        
        optim = getattr(torch.optim, optim_cfg.algo)
        assert self.cfg.train
        self.optim_gen = optim([
            dict(
                params=self.gen.parameters(), 
                lr=optim_cfg.lr,
                betas=(0.9, 0.999))])
        self.optim_dis = optim([
            dict(
                params=self.dis.parameters(),
                lr=optim_cfg.lr,
                betas=(0.9, 0.999))])
        
    def setup_scheduler(self):
        sched_cfg = self.cfg.optimizer.schedule
        if sched_cfg:
            if sched_cfg.type == 'LambdaLR':
                lr_lambda = lambda epoch: sched_cfg.gamma ** (epoch / sched_cfg.step_size)
                other_kwargs = {
                    'last_epoch':self.start_epoch - 1,
                    'lr_lambda':lr_lambda,
                    'verbose':sched_cfg.verbose}
                self.sched_gen = torch.optim.lr_scheduler.LambdaLR(
                    self.optim_gen,
                    **other_kwargs)
                self.sched_dis = torch.optim.lr_scheduler.LambdaLR(
                    self.optim_dis,
                    **other_kwargs)
            else:
                # TODO
                raise NotImplementedError
            log.info('Scheduler {0} starts from epoch {1}'.format(sched_cfg.type, self.start_epoch))
        else:
            self.sched = None
            log.info('Not scheduler specified')
            
    def save_checkpoint(self ):
        checkpoint_name = 'epoch{}_weights.pth'.format(self.curr_epoch)
        checkpoint_path = os.path.join(self.cfg.checkpoint_dir, checkpoint_name)
        log.info('Storing checkpoint in {}..'.format(checkpoint_path))
        data_to_store = {
            'gen': self.gen.state_dict(),
            'optim_gen': self.optim_gen.state_dict(),
            'dis': self.dis.state_dict(),
            'optim_dis': self.optim_dis.state_dict(),
            'state': pickle.dumps(self.stats)}
        torch.save(data_to_store, checkpoint_path)
        
    def restore_checkpoint(self):
        os.makedirs(self.cfg.checkpoint_dir, exist_ok=True)
        if self.cfg.resume and os.path.isfile(self.cfg.resume_from):
            log.info('Resuming weights from checkpoint {}..'.format(self.cfg.resume_from))
            loaded_data = torch.load(self.cfg.resume_from)
            # other states
            self.stats = pickle.loads(loaded_data['stats'])
            self.start_epoch = self.stats.epoch
            # model related
            self.gen.load_state_dict(loaded_data['gen'])
            if hasattr(self, 'dis'):
                self.dis.load_state_dict(loaded_data['dis'])
            # optimizer related
            if hasattr(self, 'optim_gen'):
                self.optim_gen.load_state_dict(loaded_data['optim_gen'])
                self.optim_gen.last_epoch = self.stats.epoch
            if hasattr(self, 'optim_dis'):
                self.optim_dis.load_state_dict(loaded_data['optim_dis'])
                self.optim_dis.last_epoch = self.stats.epoch
        elif self.cfg.resume and not os.path.isfile(self.cfg.resume_from):
            log.error('Checkpint {} not exists'.format(self.cfg.checkpoint_dir))
            raise Exception
        else:
            log.info('Starting new checkpoint')
            self.stats = Stats(["gen_psnr", "gen_l1", "gen_percept", "gen_id", "gen_gan", "dis_real", "dis_gan", "dis_fake", "loss_gen", "loss_dis", "sec/it"])
            self.start_epoch = 0

    # def image_metrics(self, 
    #                   gt:torch.Tensor, 
    #                   pred:Optional[torch.Tensor]=None):
    #     """
    #     Generate extra metrics for model evaluation
    #     Args:
    #         image (torch.Tensor): ground truth image
    #         rgb_fine (Optinal[torch.Tensor], optional): fine result. Defaults to None.
    #         rgb_coarse (Optional[torch.Tensor], optional): coarse reulst. Defaults to None.

    #     Returns:
    #         metrics_dict (Dict): generation evaluation metrics.
    #     """
        
    #     metrics_dict = {}
    #     # LIPIS pretrained model initialization
    #     if not hasattr(self, 'lpips_func'):
    #         import lpips
    #         self.lpips_func = lpips.LPIPS(net='alex').to(self.device)
    #         self.lpips_func.eval()
        
    #     # LIPIS scores
    #     from nerf_utils.utils import calc_lpips
    #     norm_image = lambda x: x * 2 - 1
    #     channel_first = lambda x: x.permute(0,3,1,2)
    #     with torch.no_grad():
    #         if rgb_coarse is not None:
    #             metrics_dict['lpips_coarse'] = calc_lpips(
    #                 norm_image(channel_first(image[None, ])), 
    #                 norm_image(channel_first(rgb_coarse)), 
    #                 self.lpips_func
    #                 ).squeeze()     
        
    #     # # SSIM scores
    #     # from nerf_utils.utils import calc_ssim
    #     # norm_image = lambda x: x * 255.
    #     # to_numpy = lambda x: x.detach().cpu().numpy()
    #     # if rgb_fine is not None:
    #     #     metrics_dict['ssim_fine'] = calc_ssim(
    #     #         norm_image(to_numpy(image)), 
    #     #         norm_image(to_numpy(rgb_fine[0])))
    #     # if rgb_coarse is not None:
    #     #     metrics_dict['ssim_coarse'] = calc_ssim(
    #     #         norm_image(to_numpy(image)), 
    #     #         norm_image(to_numpy(rgb_coarse[0])))   
        
    #     return metrics_dict  