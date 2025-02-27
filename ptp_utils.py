# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont
import cv2
from typing import Optional, Union, Tuple, List, Callable, Dict
from IPython.display import display
from tqdm.notebook import tqdm
import torch.nn.functional as F
import sys


def text_under_image(image: np.ndarray, text: str, text_color: Tuple[int, int, int] = (0, 0, 0)):
    h, w, c = image.shape
    offset = int(h * .2)
    img = np.ones((h + offset, w, c), dtype=np.uint8) * 255
    font = cv2.FONT_HERSHEY_SIMPLEX
    # font = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoMono-Regular.ttf", font_size)
    img[:h] = image
    textsize = cv2.getTextSize(text, font, 1, 2)[0]
    text_x, text_y = (w - textsize[0]) // 2, h + offset - textsize[1] // 2
    cv2.putText(img, text, (text_x, text_y ), font, 1, text_color, 2)
    return img


def view_images(images, num_rows=1, offset_ratio=0.02, centroids = None):
        if type(images) is list:
            num_empty = len(images) % num_rows
        elif images.ndim == 4:
            num_empty = images.shape[0] % num_rows
        else:
            images = [images]
            num_empty = 0

        empty_images = np.ones(images[0].shape, dtype=np.uint8) * 255
        images = [image.astype(np.uint8) for image in images] + [empty_images] * num_empty
        num_items = len(images)
        h, w, c = images[0].shape
        offset = int(h * offset_ratio)
        num_cols = num_items // num_rows

        image_ = np.ones((h * num_rows  + offset * (num_rows - 1),
                            w * num_cols + offset * (num_cols - 1), 3), dtype=np.uint8) * 255
        for i in range(num_rows):
            for j in range(num_cols):
                image_[i * (h + offset): i * (h + offset) + h:, j * (w + offset): j * (w + offset) + w] = images[
                    i * num_cols + j]
                if centroids:
                    # Draw centroid on image_[i * (h + offset): i * (h + offset) + h:, j * (w + offset): j * (w + offset) + w] from coordinates x-2, y-2 to x+2, y+2 in red
                    x, y = centroids[i * num_cols + j]
                    # Change values in image_ at coordinate range x-2, x+2, y-2, y+2 to red
                    image_[i * (h + offset) + int(y) - 2: i * (h + offset) + int(y) + 2, j * (w + offset) + int(x) - 2: j * (w + offset) + int(x) + 2] = [255, 0, 0]
        pil_img = Image.fromarray(image_)
        display(pil_img)

def get_attention_maps(attention, res, from_where, prompts, select):
    attention_maps = attention.get_average_attention()
    out = []
    num_pixels = res * res
    for location in from_where:
        for item in attention_maps[f"{location}_cross"]:
            if item.shape[1] == num_pixels:
                cross_maps = item.reshape(len(prompts), -1, res, res, item.shape[-1])[select]
                out.append(cross_maps)
    out = torch.cat(out, dim=0)
    out = out.sum(0) / out.shape[0]
    return out

# def get_attention_maps(attention_store, res, from_where, prompts, select):
#     out = []
#     num_pixels = res * res

#     for item in attention_store.attention_store["up_cross"]:
#         if item.shape[1] == num_pixels:
#             cross_maps = item.reshape(len(prompts), -1, res, res, item.shape[-1])[select]
#             out.append(cross_maps)

#     out = torch.cat(out, dim=0)
#     out = out.sum(0) / out.shape[0]
#     return out


def normalize_attention(A):
    min_val = torch.min(A)
    max_val = torch.max(A)
    return (A - min_val) / (max_val - min_val)

def get_obj_centroid(centroids, moving_obj, tokens, tokenizer):
    token_strings = [tokenizer.decode(token_id).replace(' ', '') for token_id in tokens]
    # Find the index of the word "ball" in the tokenized input
    if moving_obj in token_strings:
        index = token_strings.index(moving_obj)
        return torch.tensor(centroids[index], requires_grad=True)
    return torch.tensor([None, None])

def get_guidance_loss(target_pt, obj_cetroid):
    # l = torch.sum(torch.abs(target_pt - obj_cetroid))
    l = torch.abs(target_pt - obj_cetroid).sum()
    l.requires_grad = True
    print("l details", l, l.shape, l.requires_grad)
    return l


def diffusion_step(model, controller, latents, context, t, guidance_scale, low_resource=False, tokenizer=None, prompts=None, select=0):
    tokens = tokenizer.encode(prompts[select])
    decoder = tokenizer.decode
    if low_resource:
        noise_pred_uncond = model.unet(latents, t, encoder_hidden_states=context[0])["sample"]
        noise_prediction_text = model.unet(latents, t, encoder_hidden_states=context[1])["sample"]
    else:
        latents_input = torch.cat([latents] * 2)
        noise_pred = model.unet(latents_input, t, encoder_hidden_states=context)["sample"]
        noise_pred_uncond, noise_prediction_text = noise_pred.chunk(2)

    # cross_attention = get_attention_maps(controller, 16, ["up", "down"], prompts, select) # Do not detach using get_attention_maps, use attention_store
    # cross_attention.requires_grad = True
    res = 16
    num = res * res
    attn_maps = controller.attention_store["up_cross"]
    # cross_attention = attn_maps[-2]
    # Put attention maps of last two layers together in cross_attention map if map.shape[1] == num without using a list and reshape to (len(prompts), -1, res, res, map.shape[-1])[select] before appending to cross_attention
    # cross_attention = torch.cat([attn_map.reshape(len(prompts), -1, res, res, attn_map.shape[-1])[select] for attn_map in attn_maps if attn_map.shape[1] == num], dim=0)
    cross_attention = torch.cat((attn_maps[-2].reshape(len(prompts), -1, res, res, attn_maps[-2].shape[-1])[select], attn_maps[-1].reshape(len(prompts), -1, res, res, attn_maps[-1].shape[-1])[select]), dim=0)
    print("cross_attention shape before sum", cross_attention.shape)
    cross_attention = cross_attention.sum(0) / cross_attention.shape[0]
    # cross_attention = torch.cat([attn_map for attn_map in attn_maps if attn_map.shape[1] == num], dim=0)
    # cross_attention = cross_attention.reshape(len(prompts), -1, res, res, cross_attention.shape[-1])[select]
    print("cross_attention", cross_attention.shape)

    # for attn_map in attn_maps:
    #     if attn_map.shape[1] == num:
    #         attn_map = attn_map.reshape(len(prompts), -1, res, res, attn_map.shape[-1])[select]
    
    print("cross_attention requires Grad", cross_attention.requires_grad)
    s = 10
    # images = []
    # centroids = []
    # Athresh = normalize(sigmoid(s·(normalize(A)−0.5))) for cross attention of each token
    #for k in range(len(tokens)):
    # for k in range(3,4):
    k = 3
    image = 255 * normalize_attention(torch.sigmoid(s * (normalize_attention(cross_attention[:, :, k]) - 0.5)))
    image = image.unsqueeze(-1).expand(*image.shape, 3)

    # image = image.permute(2, 0, 1).unsqueeze(0).float()
    # image = F.interpolate(image, size=(256, 256), mode='bilinear', align_corners=False)
    # image = image.to(torch.uint8)
    # image = image.resize((256, 256))
    # print("image", image.shape)
    gray_image = torch.mean(image, dim=2, keepdim=False)
    print("gray_image", gray_image.shape)

    # image = image.numpy().astype(np.uint8)
    # image = np.array(Image.fromarray(image).resize((256, 256)))
    # print("image", image.shape)
    # gray_image = np.mean(image, axis=2)  # Shape: (256, 256)
    # print("gray_image", gray_image.shape)
    
    # Calculate the sum of elements in each row, weighted by 'h'
    weighted_sum_h = torch.sum(gray_image * torch.arange(gray_image.shape[0], dtype=torch.float32).reshape(-1, 1), axis=0)

    # Calculate the sum of elements in each column, weighted by 'w'
    weighted_sum_w = torch.sum(gray_image * torch.arange(gray_image.shape[1], dtype=torch.float32).reshape(1, -1), axis=1)

    # Calculate the centroid coordinates
    centroid_x = torch.sum(weighted_sum_w) / torch.sum(gray_image)
    centroid_y = torch.sum(weighted_sum_h) / torch.sum(gray_image)
    # print("centroid x and y shape", centroid_x.shape, centroid_y.shape)
    # centroid = torch.cat([centroid_x.reshape(1), centroid_y.reshape(1)], axis=0)
    # centroids.append(centroid)
    # image = text_under_image(image, decoder(int(tokens[k])))
    # images.append(image)

    # view_images(images=np.stack(images, axis=0),centroids=centroids)
    # target_pt = torch.tensor([gray_image.shape[0]*0.2, gray_image.shape[1]*0.8])
    target_pt_x = torch.FloatTensor([4])
    target_pt_y = torch.FloatTensor([12])
    # moving_obj = "ball"
    # obj_cetroid = get_obj_centroid(centroids, moving_obj, tokens, tokenizer)
    # obj_cetroid = centroids[3]
    # obj_cetroid.requires_grad = True
    latents.requires_grad = True
    # guidance_loss = get_guidance_loss(target_pt, obj_cetroid)
    # guidance_loss = torch.abs(target_pt - obj_cetroid).sum()
    print(target_pt_x, centroid_x)
    guidance_loss = torch.abs(target_pt_x.reshape(1) - centroid_x.reshape(1)).sum()
    print("guidance_loss", guidance_loss, guidance_loss.shape)
    #guidance_loss += torch.abs(target_pt_y.reshape(1) - centroid_y.reshape(1)).sum()
    # guidance_loss.requires_grad = True
    g_loss = torch.autograd.grad(outputs=guidance_loss, inputs=latents, allow_unused=True)
    print("g_loss", g_loss, g_loss.shape)
    print("noise_pred_uncond.shape", noise_pred_uncond.shape)
    print("latents.shape", latents.shape)
    v = 7500
    variance = torch.var(noise_pred_uncond)
    sigma = torch.sqrt(variance)
    # print("sigma", sigma)

    noise_pred = noise_pred_uncond + guidance_scale * (noise_prediction_text - noise_pred_uncond) + v*sigma*g_loss
    latents = model.scheduler.step(noise_pred, t, latents)["prev_sample"]
    latents = controller.step_callback(latents)
    return latents


def latent2image(vae, latents):
    latents = 1 / 0.18215 * latents
    image = vae.decode(latents)['sample']
    image = (image / 2 + 0.5).clamp(0, 1)
    image = image.cpu().permute(0, 2, 3, 1).numpy()
    image = (image * 255).astype(np.uint8)
    return image


def init_latent(latent, model, height, width, generator, batch_size):
    if latent is None:
        latent = torch.randn(
            (1, model.unet.in_channels, height // 8, width // 8),
            generator=generator,
        )
    latents = latent.expand(batch_size,  model.unet.in_channels, height // 8, width // 8).to(model.device)
    return latent, latents


@torch.no_grad()
def text2image_ldm(
    model,
    prompt:  List[str],
    controller,
    num_inference_steps: int = 50,
    guidance_scale: Optional[float] = 7.,
    generator: Optional[torch.Generator] = None,
    latent: Optional[torch.FloatTensor] = None,
    tokenizer = None
):
    register_attention_control(model, controller)
    height = width = 256
    batch_size = len(prompt)
    
    uncond_input = model.tokenizer([""] * batch_size, padding="max_length", max_length=77, return_tensors="pt")
    uncond_embeddings = model.bert(uncond_input.input_ids.to(model.device))[0]
    
    text_input = model.tokenizer(prompt, padding="max_length", max_length=77, return_tensors="pt")
    text_embeddings = model.bert(text_input.input_ids.to(model.device))[0]
    latent, latents = init_latent(latent, model, height, width, generator, batch_size)
    context = torch.cat([uncond_embeddings, text_embeddings])
    
    model.scheduler.set_timesteps(num_inference_steps)
    for t in tqdm(model.scheduler.timesteps):
        latents = diffusion_step(model, controller, latents, context, t, guidance_scale, tokenizer=tokenizer, prompts=prompt)
    
    image = latent2image(model.vqvae, latents)
   
    return image, latent


@torch.no_grad()
def text2image_ldm_stable(
    model,
    prompt: List[str],
    controller,
    num_inference_steps: int = 50,
    guidance_scale: float = 7.5,
    generator: Optional[torch.Generator] = None,
    latent: Optional[torch.FloatTensor] = None,
    low_resource: bool = False,
):
    register_attention_control(model, controller)
    height = width = 512
    batch_size = len(prompt)

    text_input = model.tokenizer(
        prompt,
        padding="max_length",
        max_length=model.tokenizer.model_max_length,
        truncation=True,
        return_tensors="pt",
    )
    text_embeddings = model.text_encoder(text_input.input_ids.to(model.device))[0]
    max_length = text_input.input_ids.shape[-1]
    uncond_input = model.tokenizer(
        [""] * batch_size, padding="max_length", max_length=max_length, return_tensors="pt"
    )
    uncond_embeddings = model.text_encoder(uncond_input.input_ids.to(model.device))[0]
    
    context = [uncond_embeddings, text_embeddings]
    if not low_resource:
        context = torch.cat(context)
    latent, latents = init_latent(latent, model, height, width, generator, batch_size)
    
    # set timesteps
    extra_set_kwargs = {"offset": 1}
    model.scheduler.set_timesteps(num_inference_steps, **extra_set_kwargs)
    for t in tqdm(model.scheduler.timesteps):
        latents = diffusion_step(model, controller, latents, context, t, guidance_scale, low_resource)
    
    image = latent2image(model.vae, latents)
  
    return image, latent


def register_attention_control(model, controller):
    def ca_forward(self, place_in_unet):
        to_out = self.to_out
        if type(to_out) is torch.nn.modules.container.ModuleList:
            to_out = self.to_out[0]
        else:
            to_out = self.to_out

        def forward(x, context=None, mask=None):
            batch_size, sequence_length, dim = x.shape
            h = self.heads
            q = self.to_q(x)
            is_cross = context is not None
            context = context if is_cross else x
            k = self.to_k(context)
            v = self.to_v(context)
            q = self.reshape_heads_to_batch_dim(q)
            k = self.reshape_heads_to_batch_dim(k)
            v = self.reshape_heads_to_batch_dim(v)

            sim = torch.einsum("b i d, b j d -> b i j", q, k) * self.scale

            if mask is not None:
                mask = mask.reshape(batch_size, -1)
                max_neg_value = -torch.finfo(sim.dtype).max
                mask = mask[:, None, :].repeat(h, 1, 1)
                sim.masked_fill_(~mask, max_neg_value)

            # attention, what we cannot get enough of
            attn = sim.softmax(dim=-1)
            attn = controller(attn, is_cross, place_in_unet)
            out = torch.einsum("b i j, b j d -> b i d", attn, v)
            out = self.reshape_batch_dim_to_heads(out)
            return to_out(out)

        return forward

    class DummyController:

        def __call__(self, *args):
            return args[0]

        def __init__(self):
            self.num_att_layers = 0

    if controller is None:
        controller = DummyController()

    def register_recr(net_, count, place_in_unet):
        if net_.__class__.__name__ == 'CrossAttention':
            net_.forward = ca_forward(net_, place_in_unet)
            return count + 1
        elif hasattr(net_, 'children'):
            for net__ in net_.children():
                count = register_recr(net__, count, place_in_unet)
        return count

    cross_att_count = 0
    sub_nets = model.unet.named_children()
    for net in sub_nets:
        if "down" in net[0]:
            cross_att_count += register_recr(net[1], 0, "down")
        elif "up" in net[0]:
            cross_att_count += register_recr(net[1], 0, "up")
        elif "mid" in net[0]:
            cross_att_count += register_recr(net[1], 0, "mid")

    controller.num_att_layers = cross_att_count

    
def get_word_inds(text: str, word_place: int, tokenizer):
    split_text = text.split(" ")
    if type(word_place) is str:
        word_place = [i for i, word in enumerate(split_text) if word_place == word]
    elif type(word_place) is int:
        word_place = [word_place]
    out = []
    if len(word_place) > 0:
        words_encode = [tokenizer.decode([item]).strip("#") for item in tokenizer.encode(text)][1:-1]
        cur_len, ptr = 0, 0

        for i in range(len(words_encode)):
            cur_len += len(words_encode[i])
            if ptr in word_place:
                out.append(i + 1)
            if cur_len >= len(split_text[ptr]):
                ptr += 1
                cur_len = 0
    return np.array(out)


def update_alpha_time_word(alpha, bounds: Union[float, Tuple[float, float]], prompt_ind: int,
                           word_inds: Optional[torch.Tensor]=None):
    if type(bounds) is float:
        bounds = 0, bounds
    start, end = int(bounds[0] * alpha.shape[0]), int(bounds[1] * alpha.shape[0])
    if word_inds is None:
        word_inds = torch.arange(alpha.shape[2])
    alpha[: start, prompt_ind, word_inds] = 0
    alpha[start: end, prompt_ind, word_inds] = 1
    alpha[end:, prompt_ind, word_inds] = 0
    return alpha


def get_time_words_attention_alpha(prompts, num_steps,
                                   cross_replace_steps: Union[float, Dict[str, Tuple[float, float]]],
                                   tokenizer, max_num_words=77):
    if type(cross_replace_steps) is not dict:
        cross_replace_steps = {"default_": cross_replace_steps}
    if "default_" not in cross_replace_steps:
        cross_replace_steps["default_"] = (0., 1.)
    alpha_time_words = torch.zeros(num_steps + 1, len(prompts) - 1, max_num_words)
    for i in range(len(prompts) - 1):
        alpha_time_words = update_alpha_time_word(alpha_time_words, cross_replace_steps["default_"],
                                                  i)
    for key, item in cross_replace_steps.items():
        if key != "default_":
             inds = [get_word_inds(prompts[i], key, tokenizer) for i in range(1, len(prompts))]
             for i, ind in enumerate(inds):
                 if len(ind) > 0:
                    alpha_time_words = update_alpha_time_word(alpha_time_words, item, i, ind)
    alpha_time_words = alpha_time_words.reshape(num_steps + 1, len(prompts) - 1, 1, 1, max_num_words)
    return alpha_time_words
