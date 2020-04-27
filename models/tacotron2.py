import copy
import torch
from math import sqrt
from torch import nn
from TTS.layers.tacotron2 import Encoder, Decoder, Postnet
from TTS.utils.generic_utils import sequence_mask


# TODO: match function arguments with tacotron
class Tacotron2(nn.Module):
    def __init__(self,
                 num_chars,
                 num_speakers,
                 r,
                 speaker_embedding_dim=512,
                 speaker_embedding_weights=None,
                 postnet_output_dim=80,
                 decoder_output_dim=80,
                 attn_type='original',
                 attn_win=False,
                 attn_norm="softmax",
                 prenet_type="original",
                 prenet_dropout=True,
                 forward_attn=False,
                 trans_agent=False,
                 forward_attn_mask=False,
                 location_attn=True,
                 attn_K=5,
                 separate_stopnet=True,
                 bidirectional_decoder=False):
        super(Tacotron2, self).__init__()
        self.postnet_output_dim = postnet_output_dim
        self.decoder_output_dim = decoder_output_dim
        self.num_speakers = num_speakers
        self.n_frames_per_step = r
        self.bidirectional_decoder = bidirectional_decoder
        decoder_dim = 512+speaker_embedding_dim if num_speakers > 1 else 512
        encoder_dim = 512 if num_speakers > 1 else 512
        proj_speaker_dim = 80 if num_speakers > 1 else 0
        # embedding layer
        self.embedding = nn.Embedding(num_chars, 512, padding_idx=0)
        std = sqrt(2.0 / (num_chars + 512))
        val = sqrt(3.0) * std  # uniform bounds for std
        self.embedding.weight.data.uniform_(-val, val)
        if num_speakers > 1:
            if speaker_embedding_weights:
                self.speaker_embedding = nn.Embedding.from_pretrained(torch.FloatTensor(speaker_embedding_weights))
            else:
                self.speaker_embedding = nn.Embedding(num_speakers, speaker_embedding_dim)
                self.speaker_embedding.weight.data.normal_(0, 0.3)

            self.speaker_embeddings = None
        self.encoder = Encoder(encoder_dim)
        self.decoder = Decoder(decoder_dim, self.decoder_output_dim, r, attn_type, attn_win,
                               attn_norm, prenet_type, prenet_dropout,
                               forward_attn, trans_agent, forward_attn_mask,
                               location_attn, attn_K, separate_stopnet)
        if self.bidirectional_decoder:
            self.decoder_backward = copy.deepcopy(self.decoder)
        self.postnet = Postnet(self.postnet_output_dim)

    def _init_states(self):
        self.speaker_embeddings = None

    def compute_speaker_embedding(self, speaker_ids):
        if hasattr(self, "speaker_embedding") and speaker_ids is None:
            raise RuntimeError(
                " [!] Model has speaker embedding layer but speaker_id is not provided"
            )
        if hasattr(self, "speaker_embedding") and speaker_ids is not None:
            self.speaker_embeddings = self._compute_speaker_embedding(
                speaker_ids)

    def forward(self, characters, text_lengths, mel_specs=None, speaker_ids=None):
        self._init_states()
        # compute mask for padding
        mask = sequence_mask(text_lengths).to(characters.device)
        embedded_inputs = self.embedding(characters).transpose(1, 2)
        self.compute_speaker_embedding(speaker_ids)
        encoder_outputs = self.encoder(embedded_inputs, text_lengths)
        if self.num_speakers > 1:
            encoder_outputs = self._concat_speaker_embedding(
                encoder_outputs, self.speaker_embeddings)
        decoder_outputs, alignments, stop_tokens = self.decoder(
            encoder_outputs, mel_specs, mask)
        postnet_outputs = self.postnet(decoder_outputs)
        postnet_outputs = decoder_outputs + postnet_outputs
        decoder_outputs, postnet_outputs, alignments = self.shape_outputs(
            decoder_outputs, postnet_outputs, alignments)
        if self.bidirectional_decoder:
            decoder_outputs_backward, alignments_backward = self._backward_inference(mel_specs, encoder_outputs, mask)
            return decoder_outputs, postnet_outputs, alignments, stop_tokens, decoder_outputs_backward, alignments_backward
        return decoder_outputs, postnet_outputs, alignments, stop_tokens

    @staticmethod
    def shape_outputs(mel_outputs, mel_outputs_postnet, alignments):
        mel_outputs = mel_outputs.transpose(1, 2)
        mel_outputs_postnet = mel_outputs_postnet.transpose(1, 2)
        return mel_outputs, mel_outputs_postnet, alignments

    

    @torch.no_grad()
    def inference(self, characters, speaker_ids=None):
        embedded_inputs = self.embedding(characters).transpose(1, 2)
        self._init_states()
        self.compute_speaker_embedding(speaker_ids)
        '''
        # if encoder receive speaker embedding
        if self.num_speakers > 1:
            embedded_inputs = self._concat_speaker_embedding(embedded_inputs.transpose(1,2),
                                                    self.speaker_embeddings).transpose(1,2)'''
        encoder_outputs = self.encoder.inference(embedded_inputs)
        if self.num_speakers > 1:
            encoder_outputs = self._concat_speaker_embedding(
                encoder_outputs, self.speaker_embeddings)
        mel_outputs, alignments, stop_tokens = self.decoder.inference(
            encoder_outputs)
        mel_outputs_postnet = self.postnet(mel_outputs)
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet
        mel_outputs, mel_outputs_postnet, alignments = self.shape_outputs(
            mel_outputs, mel_outputs_postnet, alignments)
        return mel_outputs, mel_outputs_postnet, alignments, stop_tokens

    def inference_truncated(self, text, speaker_ids=None):
        """
        Preserve model states for continuous inference
        """
        self.compute_speaker_embedding(speaker_ids)
        embedded_inputs = self.embedding(text).transpose(1, 2)
        '''
        # if encoder receive speaker embedding
        if self.num_speakers > 1:
            embedded_inputs = self._concat_speaker_embedding(embedded_inputs.transpose(1,2),
                                                    self.speaker_embeddings).transpose(1,2)'''
        encoder_outputs = self.encoder.inference_truncated(embedded_inputs)
        if self.num_speakers > 1:
            encoder_outputs = self._concat_speaker_embedding(
                encoder_outputs, self.speaker_embeddings)
        mel_outputs, alignments, stop_tokens = self.decoder.inference_truncated(
            encoder_output)
        mel_outputs_postnet = self.postnet(mel_outputs)
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet
        mel_outputs, mel_outputs_postnet, alignments = self.shape_outputs(
            mel_outputs, mel_outputs_postnet, alignments)
        return mel_outputs, mel_outputs_postnet, alignments, stop_tokens

    def _backward_inference(self, mel_specs, encoder_outputs, mask):
        decoder_outputs_b, alignments_b, _ = self.decoder_backward(
            encoder_outputs, torch.flip(mel_specs, dims=(1,)), mask)
        decoder_outputs_b = decoder_outputs_b.transpose(1, 2)
        return decoder_outputs_b, alignments_b

    def _compute_speaker_embedding(self, speaker_ids):
        speaker_embeddings = self.speaker_embedding(speaker_ids)
        return speaker_embeddings.unsqueeze_(1)

    @staticmethod
    def _add_speaker_embedding(outputs, speaker_embeddings):
        speaker_embeddings_ = speaker_embeddings.expand(
            outputs.size(0), outputs.size(1), -1)
        outputs = outputs + speaker_embeddings_
        return outputs

    @staticmethod
    def _concat_speaker_embedding(outputs, speaker_embeddings):
        speaker_embeddings_ = speaker_embeddings.expand(
            outputs.size(0), outputs.size(1), -1)
        outputs = torch.cat([outputs, speaker_embeddings_], dim=-1)
        return outputs

