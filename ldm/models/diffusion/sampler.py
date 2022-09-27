'''
ldm.models.diffusion.sampler

Base class for ldm.models.diffusion.ddim, ldm.models.diffusion.ksampler, etc

'''
import torch
import numpy as np
from tqdm import tqdm
from functools import partial
from ldm.dream.devices import choose_torch_device

from ldm.modules.diffusionmodules.util import (
    make_ddim_sampling_parameters,
    make_ddim_timesteps,
    noise_like,
    extract_into_tensor,
)

class Sampler(object):
    def __init__(self, model, schedule='linear', steps=None, device=None, **kwargs):
        self.model = model
        self.ddpm_num_timesteps = steps
        self.schedule = schedule
        self.device   = device or choose_torch_device()

    def register_buffer(self, name, attr):
        if type(attr) == torch.Tensor:
            if attr.device != torch.device(self.device):
                attr = attr.to(torch.float32).to(torch.device(self.device))
        setattr(self, name, attr)

    # make_schedule() is called prior to sampling() and can be used
    # to initialize any state variables. It was originally used in
    # the DDIM scheduler, and enhance all the ddim* variable names
    @torch.no_grad()
    def make_schedule(
            self,
            ddim_num_steps,
            ddim_discretize='uniform',
            ddim_eta=0.0,
            model=None,
            verbose=False,
    ):
        if model is not None:
            self.model = model
        self.ddim_num_steps = ddim_num_steps
        self.ddim_eta = ddim_eta
        self.ddim_discretize = ddim_discretize
        self.verbose = verbose
        self.ddim_timesteps = make_ddim_timesteps(
            ddim_discr_method=ddim_discretize,
            num_ddim_timesteps=ddim_num_steps,
            num_ddpm_timesteps=self.ddpm_num_timesteps,
            verbose=verbose,
        )

    @torch.no_grad()
    def sample(
        self,
        S,          # S is steps
        batch_size,
        shape,
        conditioning=None,
        callback=None,
        normals_sequence=None,
        img_callback=None,
        quantize_x0=False,
        eta=0.0,
        mask=None,
        x0=None,
        temperature=1.0,
        noise_dropout=0.0,
        score_corrector=None,
        corrector_kwargs=None,
        verbose=False,
        x_T=None,
        log_every_t=100,
        unconditional_guidance_scale=1.0,
        unconditional_conditioning=None,
        # this has to come in the same format as the conditioning, # e.g. as encoded tokens, ...
        **kwargs,
    ):

        ts = self.get_timesteps(S)

        # sampling
        C, H, W = shape
        shape = (batch_size, C, H, W)
        samples, intermediates = self.do_sampling(
            conditioning,
            shape,
            timesteps=ts,
            callback=callback,
            img_callback=img_callback,
            quantize_denoised=quantize_x0,
            mask=mask,
            x0=x0,
            ddim_use_original_steps=False,
            noise_dropout=noise_dropout,
            temperature=temperature,
            score_corrector=score_corrector,
            corrector_kwargs=corrector_kwargs,
            x_T=x_T,
            log_every_t=log_every_t,
            unconditional_guidance_scale=unconditional_guidance_scale,
            unconditional_conditioning=unconditional_conditioning,
            steps=S,
        )
        return samples, intermediates

    @torch.no_grad()
    def stochastic_encode(self, x0, t, use_original_steps=False, noise=None):
        # fast, but does not allow for exact reconstruction
        # t serves as an index to gather the correct alphas
        if use_original_steps:
            sqrt_alphas_cumprod = self.sqrt_alphas_cumprod
            sqrt_one_minus_alphas_cumprod = self.sqrt_one_minus_alphas_cumprod
        else:
            sqrt_alphas_cumprod = torch.sqrt(self.ddim_alphas)
            sqrt_one_minus_alphas_cumprod = self.ddim_sqrt_one_minus_alphas

        if noise is None:
            noise = torch.randn_like(x0)
        return (
            extract_into_tensor(sqrt_alphas_cumprod, t, x0.shape) * x0
            + extract_into_tensor(sqrt_one_minus_alphas_cumprod, t, x0.shape)
            * noise
        )

    def do_sampling(
            self,
            cond,
            shape,
            timesteps=None,
            x_T=None,
            ddim_use_original_steps=False,
            callback=None,
            quantize_denoised=False,
            mask=None,
            x0=None,
            img_callback=None,
            log_every_t=100,
            temperature=1.0,
            noise_dropout=0.0,
            score_corrector=None,
            corrector_kwargs=None,
            unconditional_guidance_scale=1.0,
            unconditional_conditioning=None,
            steps=None,
    ):
        b = shape[0]
        time_range = (
            list(reversed(range(0, timesteps)))
            if ddim_use_original_steps
            else np.flip(timesteps)
        )
        total_steps=steps

        iterator = tqdm(
            time_range,
            desc=f'{self.__class__.__name__}',
            total=total_steps,
            dynamic_ncols=True,
        )
        old_eps = []
        self.prepare_to_sample(t_enc=total_steps)
        img = self.get_initial_image(x_T,shape,total_steps)

        # probably don't need this at all
        intermediates = {'x_inter': [img], 'pred_x0': [img]}

        for i, step in enumerate(iterator):
            index = total_steps - i - 1
            ts = torch.full(
                (b,),
                step,
                device=self.device,
                dtype=torch.long
            )
            ts_next = torch.full(
                (b,),
                time_range[min(i + 1, len(time_range) - 1)],
                device=self.device,
                dtype=torch.long,
            )

            if mask is not None:
                assert x0 is not None
                img_orig = self.model.q_sample(
                    x0, ts
                )  # TODO: deterministic forward pass?
                img = img_orig * mask + (1.0 - mask) * img

            outs = self.p_sample(
                img,
                cond,
                ts,
                index=index,
                use_original_steps=ddim_use_original_steps,
                quantize_denoised=quantize_denoised,
                temperature=temperature,
                noise_dropout=noise_dropout,
                score_corrector=score_corrector,
                corrector_kwargs=corrector_kwargs,
                unconditional_guidance_scale=unconditional_guidance_scale,
                unconditional_conditioning=unconditional_conditioning,
                old_eps=old_eps,
                t_next=ts_next,
            )
            img, pred_x0, e_t = outs

            old_eps.append(e_t)
            if len(old_eps) >= 4:
                old_eps.pop(0)
            if callback:
                callback(i)
            if img_callback:
                img_callback(img)

            if index % log_every_t == 0 or index == total_steps - 1:
                intermediates['x_inter'].append(img)
                intermediates['pred_x0'].append(pred_x0)

        return img, intermediates

    def get_initial_image(self,x_T,shape,timesteps=None):
        if x_T is None:
            return torch.randn(shape, device=self.device)
        else:
            return x_T
    
    def p_sample(
            self,
            img,
            cond,
            ts,
            index,
            repeat_noise=False,
            use_original_steps=False,
            quantize_denoised=False,
            temperature=1.0,
            noise_dropout=0.0,
            score_corrector=None,
            corrector_kwargs=None,
            unconditional_guidance_scale=1.0,
            unconditional_conditioning=None,
            old_eps=None,
            t_next=None,
            steps=None,
    ):
        raise NotImplementedError("p_sample() must be implemented in a descendent class")

    def prepare_to_sample(self,t_enc,**kwargs):
        '''
        Hook that will be called right before the very first invocation of p_sample()
        to allow subclass to do additional initialization. t_enc corresponds to the actual
        number of steps that will be run, and may be less than total steps if img2img is
        active.
        '''
        pass

    def get_timesteps(self,ddim_steps):
        '''
        The ddim and plms samplers work on timesteps. This method is called after
        ddim_timesteps are created in make_schedule(), and selects the portion of
        timesteps that will be used for sampling, depending on the t_enc in img2img.
        This method is implemented in dp_sampler.py.
        '''
        return None
    
