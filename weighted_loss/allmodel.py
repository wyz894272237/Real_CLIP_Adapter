import torch
import torch.utils.checkpoint
from torch import nn
from transformers import AutoProcessor, CLIPModel, CLIPTextModel, CLIPVisionModel, CLIPVisionModelWithProjection,CLIPTextModelWithProjection
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm
import torch.nn.functional as F
import torch.distributed as dist

import numpy as np
from transformers.adapters import AdapterConfig, PrefixTuningConfig, LoRAConfig, IA3Config, MAMConfig, UniPELTConfig

from mask_generator import MaskGenerator

configs = {
    "adapter": AdapterConfig(mh_adapter=True, output_adapter=True, reduction_factor=16, non_linearity="relu"),
    "prefix": PrefixTuningConfig(flat=False, prefix_length=30),
    "LoRA": LoRAConfig(r=8, alpha=16),
    "IA3": IA3Config(),
    "mam": MAMConfig(),
    "unipelt": UniPELTConfig()
}


class CLIPOutput:
    def __init__(self, loss_recon,loss_mlm, clip_loss, loss, text_embeds, image_embeds, mask):
        # self.x_rec = x_rec
        # self.loss_recon = loss_recon
        # self.loss = loss
        # self.text_embeds = text_embeds
        # self.image_embeds = image_embeds
        # self.mask = mask
        self.output = {
            # 'x_rec': x_rec, 
            'loss_recon': loss_recon,
            "loss_mlm": loss_mlm,
            'clip_loss': clip_loss,
            'loss': loss,
            'text_embeds': text_embeds,
            'image_embeds': image_embeds,
            'mask': mask
        }

class GatherLayer(torch.autograd.Function):
    """
    Gather tensors from all workers with support for backward propagation:
    This implementation does not cut the gradients as torch.distributed.all_gather does.
    """
    @staticmethod
    def forward(ctx, x):
        output = [torch.zeros_like(x) for _ in range(dist.get_world_size())]
        dist.all_gather(output, x)
        return tuple(output)
    @staticmethod
    def backward(ctx, *grads):
        all_gradients = torch.stack(grads)
        dist.all_reduce(all_gradients)
        return all_gradients[dist.get_rank()]
    
def gather_features(
        image_features,
        text_features,
):
    gathered_image_features = GatherLayer.apply(image_features)
    gathered_text_features = GatherLayer.apply(text_features)
    all_image_features = torch.cat(gathered_image_features)
    all_text_features = torch.cat(gathered_text_features)

    return all_image_features, all_text_features

# The implementation code is modified from open_clip (https://github.com/mlfoundations/open_clip.git)
class ClipLoss(nn.Module):

    def __init__(
            self,
            cache_labels=False,
            rank=0,
            world_size=1,
    ):
        super().__init__()
        self.cache_labels = cache_labels
        self.rank = rank
        self.world_size = world_size

        self.prev_num_logits = 0
        self.labels = {}

    def forward(self, image_features, text_features, logit_scale):
        device = image_features.device
        if self.world_size > 1:
            all_image_features, all_text_features = gather_features(
                image_features, text_features
            )

            logits_per_image = logit_scale * image_features @ all_text_features.T
            logits_per_text = logit_scale * text_features @ all_image_features.T
        else:
            logits_per_image = logit_scale * image_features @ text_features.T
            logits_per_text = logit_scale * text_features @ image_features.T

        # calculated ground-truth and cache if enabled
        num_logits = logits_per_image.shape[0]
        if self.prev_num_logits != num_logits or device not in self.labels:
            labels = torch.arange(num_logits, device=device, dtype=torch.long)
            if self.world_size > 1:
                labels = labels + num_logits * self.rank
            if self.cache_labels:
                self.labels[device] = labels
                self.prev_num_logits = num_logits
        else:
            labels = self.labels[device]

        total_loss = (
            F.cross_entropy(logits_per_image, labels) +
            F.cross_entropy(logits_per_text, labels)
            ) / 2
        return total_loss, logits_per_image, logits_per_text


class CLIPVisionClassification(nn.Module):

    def __init__(self, input_size, class_num, dropout):
        super().__init__()
        self.clipvision = CLIPVisionModelWithProjection.from_pretrained("openai/clip-vit-base-patch16")
        self.classifer = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(input_size, input_size),
            nn.Tanh(),
            nn.Dropout(dropout),
            nn.Linear(input_size, class_num)
        )

    def forward(self, inputs, **kwargs):
        outputs = self.clipvision(**inputs)
        x = outputs.pooler_output
        x = self.classifer(x)
        return x


class CLIPVisionMasked(nn.Module):
    def __init__(self, dropout_rate):
        super().__init__()
        self.clipvision = CLIPVisionModelWithProjection.from_pretrained("openai/clip-vit-base-patch16")

        self.embed_dim = self.clipvision.vision_model.config.hidden_size

        self.mask_token = nn.Parameter(torch.zeros(1, 1, self.embed_dim))
        self.pos_drop = nn.Dropout(dropout_rate)

    def forward_masked(self, pixel_values, mask):
        if mask is None:
            raise ValueError("The mask is None")

        patch_embeds = self.clipvision.vision_model.embeddings.patch_embedding(pixel_values)
        patch_embeds = patch_embeds.flatten(2).transpose(1, 2)
        B, L, _, = patch_embeds.shape

        # Pay attention here
        mask_token = self.mask_token.expand(B, L, -1)
        w = mask.flatten(1).unsqueeze(-1).type_as(mask_token)
        patch_embeds = patch_embeds * (1 - w) + mask_token * w

        class_embeds = self.clipvision.vision_model.embeddings.class_embedding.expand(B, 1, -1)
        embeddings = torch.cat((class_embeds, patch_embeds), dim=1)
        embeddings = embeddings + self.clipvision.vision_model.embeddings.position_embedding(self.clipvision.vision_model.embeddings.position_ids)

        embeddings = self.clipvision.vision_model.pre_layrnorm(embeddings)
        embeddings = self.pos_drop(embeddings)

        encoder_outputs = self.clipvision.vision_model.encoder(inputs_embeds=embeddings)
        last_hidden_state = encoder_outputs[0]
        outputs = last_hidden_state[:, 1:, :]
        img_emb = self.clipvision.vision_model.post_layernorm(outputs)  # [64, 196, 768]
        pooled_output = last_hidden_state[:, 0, :]
        pooled_output = self.clipvision.vision_model.post_layernorm(pooled_output)

        B, L, C = img_emb.shape
        H = W = int(L ** 0.5)
        img_emb = img_emb.permute(0, 2, 1).reshape(B, C, H, W)  # [64, 768, 14, 14]

        return img_emb, pooled_output
    
    def forward_unmasked(self, pixel_values):
        inputs = {}
        inputs["pixel_values"] = pixel_values
        image_embeds = self.clipvision(**inputs).image_embeds
        return image_embeds

    def forward(self, pixel_values, mask):
        masked_img_emb,masked_pooled_output = self.forward_masked(pixel_values, mask)
        # image_embeds = self.forward_unmasked(pixel_values)
        
        return masked_img_emb#, image_embeds
        


class CLIPWeightedLOSS(nn.Module):
    def __init__(self, encoder, encoder_stride, weight):
        super().__init__()
        self.weight = weight  # cliploss weight
        self.encoder = encoder  # CLIPVisionMasked
        self.encoder_stride = encoder_stride
        self.decoder = nn.Sequential(
            nn.Conv2d(
                in_channels=self.encoder.embed_dim,
                out_channels=self.encoder.embed_dim, kernel_size=3, padding=1),
            nn.BatchNorm2d(self.encoder.embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=self.encoder.embed_dim,
                out_channels=self.encoder_stride ** 2 * 3, kernel_size=1),
            nn.PixelShuffle(self.encoder_stride),
        )
        

        # add adapter
        vision_configname = "LoRA"
        # checkpoint_dir = '/home/jingmin/Real_CLIP_Adapter-old/Vision/Log/LoRA_mim_2023-04-14_01-34-59/ckpt_epoch_90'
        # self.encoder.clipvision.load_adapter(checkpoint_dir)
        self.encoder.clipvision.add_adapter(vision_configname, config=configs[vision_configname])
        self.encoder.clipvision.train_adapter(vision_configname)
        self.encoder.clipvision.set_active_adapters(vision_configname)
        # self.visual_projection = self.encoder.clipvision.visual_projection#nn.Linear(768, 512, bias=False)

        text_configname = "mam"
        self.cliptext = CLIPTextModelWithProjection.from_pretrained("../clip-vit-base-patch16",ignore_mismatched_sizes=True)
        self.cliptext.add_adapter(text_configname, config=configs[text_configname])
        self.cliptext.train_adapter(text_configname)
        self.cliptext.set_active_adapters(text_configname)

        self.textdecoder = nn.Sequential(
            nn.Linear(512, 512),
            nn.GELU(),
            nn.LayerNorm(512,eps=1e-5),
            nn.Linear(512, 49409),
        )
        self.mlmcriterion = nn.CrossEntropyLoss()

        self.criterion = ClipLoss()
        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        self.mask_generator = MaskGenerator(input_size=224, mask_patch_size=32,
                                            model_patch_size=16, mask_ratio=0.6)
        


    def forward(self, input_ids, pixel_values, attention_mask, masked_ids=None):
        # device = "cuda" if torch.cuda.is_available() else "cpu"

        if masked_ids == None:
            masked_ids = input_ids.clone()
        mask = [self.mask_generator() for a in range(pixel_values.shape[0])]

        mask = torch.tensor(np.array(mask)).to(pixel_values.device)
        # print('pixel:', pixel_values.device, 'mask', mask.device)

        z = self.encoder(pixel_values, mask)
        x_rec = self.decoder(z)
        # vision_outputs = self.visual_projection(pooled_output)
        text_input = {}
        text_input['input_ids'] = input_ids
        text_input['attention_mask'] = attention_mask
        text_outputs = self.cliptext(**text_input)


        vision_input = {}
        vision_input['pixel_values'] = pixel_values
        vision_outputs = self.encoder.clipvision(**vision_input)


        image_embeds = vision_outputs.image_embeds #/ vision_outputs.image_embeds.norm(p=2, dim=-1, keepdim=True)
        text_embeds = text_outputs.text_embeds #/ text_outputs.text_embeds.norm(p=2, dim=-1, keepdim=True)

        clip_loss, logits_per_image, logits_per_text = self.criterion(image_embeds, text_embeds, self.logit_scale)

        mask = mask.repeat_interleave(16, 1).repeat_interleave(16, 2).unsqueeze(1).contiguous()
        loss_recon = F.l1_loss(pixel_values, x_rec, reduction='none')
        loss_recon = (loss_recon * mask).sum() / (mask.sum() + 1e-5) / 3

        mask_outputs = self.cliptext(
            input_ids=masked_ids,
            attention_mask=attention_mask
        )

        mask_logits = self.textdecoder(mask_outputs.last_hidden_state)
        labels = torch.where(masked_ids == 49408, input_ids, -100).to(mask_logits.device)
        loss_mlm = self.mlmcriterion(mask_logits.view(-1, 49409), labels.view(-1))


        loss = clip_loss  + loss_mlm
        

        output = CLIPOutput(
            # x_rec = x_rec,
            loss_recon=loss_recon,  # reconstruct loss
            loss_mlm=loss_mlm,  # mlm loss
            clip_loss=clip_loss,  # clip loss
            loss=loss,
            text_embeds=text_embeds,
            image_embeds=image_embeds,
            mask=mask
        ).output

        return output
