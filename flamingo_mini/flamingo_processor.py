from PIL import Image
from typing import List, Tuple, Union, Optional
import torch
# import numpy as np

# from transformers import CLIPVisionModel, CLIPProcessor, GPT2Tokenizer
from .configuration_flamingo import FlamingoConfig, is_lm_supported
from .utils import unzip


class FlamingoProcessor:
    """ 
    FlamingoProcessor offers functions to preprocess the raw data (images and text).
    Wrapper around a transformer GPT-2 tokenizer and a clip processor.
    """

    def __init__(self,
                 config: FlamingoConfig,
                 load_tokenizer: bool = True,
                 load_vision_processor: bool = False,
                 output_captions=False,
                 ):
        
        self.config = config
        
        assert is_lm_supported(self.config.lm), 'this language model is not supported.'
        
        self.output_captions = output_captions
        
        if load_vision_processor:
            from transformers import CLIPVisionModel, CLIPProcessor

            self.vision_processor = CLIPProcessor.from_pretrained(config.clip_model_type)
            self.vision_model = CLIPVisionModel.from_pretrained(config.clip_model_type)
        else:
            self.vision_processor = None
            self.vision_model = None
        
        if load_tokenizer:
            from transformers import GPT2Tokenizer
            
            if config.lm.startswith('gpt2'):
                self.tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
            elif config.lm.startswith('facebook/opt'):
                self.tokenizer = GPT2Tokenizer.from_pretrained('facebook/opt-30b')
            
            self.eoc_token = '<EOC>'
            self.tokenizer.add_bos_token = True
            self.tokenizer.pad_token = self.tokenizer.eos_token
            self.tokenizer.add_tokens(self.eoc_token)

            # find the start token for "<image>". " <" is 1279, "<" is 27
            # => use the latter as in the text there is "...<EOC><image>example text...", so no
            # whitespace before the "<" of "<image>"
            # the encoded "<" token-id is different if there is a whitespace before.
            #        with ws    without
            # gpt-2:  1279         27
            # opt:   28696      51552
            self.leq_ids = [
                self.tokenizer.encode("<")[-1],
                self.tokenizer.encode(" <")[-1]
            ]

    def encode_text(self, text: Union[str, List[str]], device: torch.device = None) -> Tuple[torch.LongTensor, torch.BoolTensor, torch.LongTensor]:
        result = self.tokenizer(text, return_tensors='pt', padding=True)
        media_locs = self.get_media_locations(result.input_ids)

        return result.input_ids.to(device), media_locs.to(device), result.attention_mask.to(device)
    
    def prepare_caption(self, caption: str) -> str:
        # <BOS> token is added automatically by the tokenizer.
        # <EOS> token is not.
        return "<image>" + caption + self.eoc_token + self.tokenizer.eos_token
            
    def prepare_captions(self, captions: List[str]) -> List[str]:
        """preparation function for the conceptual captions dataset. """
        return [self.prepare_caption(c) for c in captions]
        
    def _remove_tags(self, text: str) -> str:
        for s in ('<image>', self.tokenizer.eos_token, self.eoc_token, self.tokenizer.pad_token):
            text = text.replace(s, '')
        return text.strip()
    
    def remove_tags(self, text: Union[str, List[str]]) -> str:
        if isinstance(text, str):
            return self._remove_tags(text)
        else:
            return [self._remove_tags(t) for t in text]
    
    def get_media_locations(self, input_ids: torch.Tensor) -> torch.BoolTensor:
        return torch.stack([(input_ids == leq_id) for leq_id in self.leq_ids]).sum(0)
    
    def preprocess_images(self, images: List[Image.Image]):
        """
        :param images: a list of PIL image instances
        :return: Tensor of shape [n_images, width, height, depth]
        """
        return self.vision_processor(images=images, return_tensors="pt", padding=True)

    def extract_features(self, images: Union[Image.Image, List[Image.Image]], device=None) -> torch.FloatTensor:
        
        if self.vision_processor is None or self.vision_model is None:
            raise ValueError("flamingo processor not initialized with vision processor")
        
        if isinstance(images, Image.Image):
            images = [images]
        
        pixels = self.preprocess_images(images)
        pixels = pixels['pixel_values']
        pixels = pixels.to(device)
        features = self.vision_model(pixels)
        features = features.last_hidden_state
        
        return features
    
    def collate_fn(self, batch):
        """ assumes the data is coming from PretokenizedConceptualDS
        
        collate_fn turns a list of features and a list of tokenized captions.
        it returns a tensor of features, a tensor of tokenized captions (padded), attention mask, and media_locations
        """
        
        # a list of features, and a list of tensors of different length
        features, tokenized_captions, captions = unzip(batch)
        features = torch.stack(features)
        
        b = len(tokenized_captions)
        l = max([len(tc) for tc in tokenized_captions])
        
        # prepare token ids and mask
        input_ids = torch.full((b, l), self.tokenizer.pad_token_id, dtype=torch.int64)
        mask = torch.zeros((b, l), dtype=torch.int64)

        for i, row in enumerate(tokenized_captions):
            input_ids[i, :len(row)] = torch.from_numpy(row)
            mask[i, :len(row)] = 1
       
        media_locs = self.get_media_locations(input_ids)        
        
        if self.output_captions:
            return features, input_ids, mask, media_locs, captions
        else:
            return features, input_ids, mask, media_locs
    
