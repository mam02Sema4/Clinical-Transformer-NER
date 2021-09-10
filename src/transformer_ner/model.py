#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
The current implementation has repeated code but will guarantee the performance for each model.
"""

from transformers import (BertConfig,  BertModel, BertPreTrainedModel,
                          RobertaModel, RobertaConfig, PreTrainedModel,
                          XLNetModel, XLNetPreTrainedModel, XLNetConfig,
                          AlbertModel, AlbertConfig,
                          DistilBertConfig, DistilBertModel,
                          ALBERT_PRETRAINED_MODEL_ARCHIVE_LIST,
                          DISTILBERT_PRETRAINED_MODEL_ARCHIVE_LIST,
                          XLNET_PRETRAINED_MODEL_ARCHIVE_LIST,
                          ROBERTA_PRETRAINED_MODEL_ARCHIVE_LIST,
                          BERT_PRETRAINED_MODEL_ARCHIVE_LIST,
                          BartConfig, BartModel, ElectraForTokenClassification,
                          ElectraModel, XLNetForTokenClassification, AlbertPreTrainedModel,
                          RobertaForTokenClassification, LongformerForTokenClassification, LongformerModel,
                          DebertaModel, DebertaPreTrainedModel)
from torch import nn
import torch.nn.functional as F
import torch

from model_utils import FocalLoss, _calculate_loss


class MLP(nn.Module):
    def __init__(self, input_dim, output_dim, hidden_dim=0, num_hidden_layers=0):
        super().__init__()
        self.weight = None
        # TODO: test Relu and LeakyRelu (negative_slope=0.1) besides GELU as linear activation
        # TODO: test if dropout need (SharedDropout)
        if num_hidden_layers and hidden_dim:
            # if num_hidden_layers = 1, then we have two layers
            layers = []
            for i in range(num_hidden_layers):
                if i == 0:
                    layers.append(nn.Linear(input_dim, hidden_dim))
                else:
                    layers.append(nn.Linear(hidden_dim, hidden_dim))
                # should test Relu and LeakyRelu (negative_slope=0.1)
                layers.append(nn.GELU())
            self.weight = nn.Sequential(*layers, nn.Linear(hidden_dim, output_dim), nn.GELU())
        else:
            # only one linear layer
            self.weight = nn.Sequential(nn.Linear(input_dim, output_dim), nn.GELU())

    def forward(self, x):
        return self.weight(x)


class Biaffine(nn.Module):
    def __init__(self, input_dim, output_dim, bias_x=True, bias_y=True):
        super().__init__()
        self.bx = bias_x
        self.by = bias_y
        self.U = torch.nn.Parameter(
            torch.Tensor(input_dim + int(bias_x), output_dim, input_dim + int(bias_y)))
        # TODO: use normal init; we can test other init method: xavier, kaiming, ones
        nn.init.normal_(self.U)

    def forward(self, x, y):
        # add bias
        if self.bx:
            x = torch.cat([x, torch.ones_like(x[..., :1])], dim=-1)
        if self.by:
            y = torch.cat([y, torch.ones_like(y[..., :1])], dim=-1)

        """
        t1: [b, s, v]
        t2: [b, s, v]
        U: [v, o, v]

        m = t1*U => [b,s,o,v] => [b, s*o, v]
        m*t2.T => [b, s*o, v] * [b, v, s] => [b, s, o, s] => [b, s, s, o]: this is the mapping table
        """
        biaffine_mappings = torch.einsum('bxi,ioj,byj->bxyo', x, self.U, y)

        return biaffine_mappings


class BiaffineNER(nn.Module):
    """
        ref:
            https://aclanthology.org/2020.acl-main.577.pdf
            https://github.com/geasyheart/biaffine_ner.git
    """
    def __init__(self, config):
        super().__init__()
        # TODO: option to use both bert output last and second last hidden states
        # mlp_input_dim = config.hidden_size if config.include_only_bert_last_hidden else config.hidden_size*2
        mlp_input_dim = config.hidden_size
        mlp_output_dim = config.mlp_dim if config.mlp_dim > 0 else config.hidden_size
        self.ffnns = MLP(mlp_input_dim, mlp_output_dim)  # ffnns: feed forward neural network start
        self.ffnne = MLP(mlp_input_dim, mlp_output_dim)  # ffnne: feed forward neural network end
        self.biaffine = Biaffine(mlp_output_dim, config.num_labels)
        self.num_labels = config.num_labels
        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

    def forward(self, x, attention_mask=None, label_ids=None):
        s_logits = self.ffnns(x)
        e_logits = self.ffnne(x)
        logits = self.biaffine(s_logits, e_logits)

        loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class Transformer_CRF(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.num_labels = config.num_labels
        self.start_label_id = config.label2idx['CLS']
        self.transitions = nn.Parameter(torch.randn(self.num_labels, self.num_labels), requires_grad=True)
        self.log_alpha = nn.Parameter(torch.zeros(1, 1, 1), requires_grad=False)
        self.score = nn.Parameter(torch.zeros(1, 1), requires_grad=False)
        self.log_delta = nn.Parameter(torch.zeros(1, 1, 1), requires_grad=False)
        self.psi = nn.Parameter(torch.zeros(1, 1, 1), requires_grad=False)
        self.path = nn.Parameter(torch.zeros(1, 1, dtype=torch.long), requires_grad=False)

    @staticmethod
    def log_sum_exp_batch(log_Tensor, axis=-1):
        # shape (batch_size,n,m)
        sum_score = torch.exp(log_Tensor - torch.max(log_Tensor, axis)[0].view(log_Tensor.shape[0], -1, 1)).sum(axis)
        return torch.max(log_Tensor, axis)[0] + torch.log(sum_score)

    def reset_layers(self):
        self.log_alpha = self.log_alpha.fill_(0.)
        self.score = self.score.fill_(0.)
        self.log_delta = self.log_delta.fill_(0.)
        self.psi = self.psi.fill_(0.)
        self.path = self.path.fill_(0)

    def forward(self, feats, label_ids):
        forward_score = self._forward_alg(feats)
        max_logLL_allz_allx, path, gold_score = self._crf_decode(feats, label_ids)
        loss = torch.mean(forward_score - gold_score)
        self.reset_layers()
        return path, max_logLL_allz_allx, loss

    def _forward_alg(self, feats):
        """alpha-recursion or forward recursion; to compute the partition function"""
        # feats -> (batch size, num_labels)
        seq_size = feats.shape[1]
        batch_size = feats.shape[0]
        log_alpha = self.log_alpha.expand(batch_size, 1, self.num_labels).clone().fill_(-10000.)
        log_alpha[:, 0, self.start_label_id] = 0
        for t in range(1, seq_size):
            log_alpha = (self.log_sum_exp_batch(self.transitions + log_alpha, axis=-1) + feats[:, t]).unsqueeze(1)
        return self.log_sum_exp_batch(log_alpha)

    def _crf_decode(self, feats, label_ids):
        seq_size = feats.shape[1]
        batch_size = feats.shape[0]

        batch_transitions = self.transitions.expand(batch_size, self.num_labels, self.num_labels)
        batch_transitions = batch_transitions.flatten(1)
        score = self.score.expand(batch_size, 1)

        log_delta = self.log_delta.expand(batch_size, 1, self.num_labels).clone().fill_(-10000.)
        log_delta[:, 0, self.start_label_id] = 0
        psi = self.psi.expand(batch_size, seq_size, self.num_labels).clone()

        for t in range(1, seq_size):
            batch_trans_score = batch_transitions.gather(
                -1, (label_ids[:, t] * self.num_labels + label_ids[:, t-1]).view(-1, 1))
            temp_score = feats[:, t].gather(-1, label_ids[:, t].view(-1, 1)).view(-1, 1)
            score = score + batch_trans_score + temp_score

            log_delta, psi[:, t] = torch.max(self.transitions + log_delta, -1)
            log_delta = (log_delta + feats[:, t]).unsqueeze(1)

        # trace back
        path = self.path.expand(batch_size, seq_size).clone()
        max_logLL_allz_allx, path[:, -1] = torch.max(log_delta.squeeze(), -1)
        for t in range(seq_size-2, -1, -1):
            path[:, t] = psi[:, t+1].gather(-1, path[:, t+1].view(-1, 1)).squeeze()

        return max_logLL_allz_allx, path, score


class BertNerModel(BertPreTrainedModel):
    """
    model architecture:
      (bert): BertModel
      (dropout): Dropout(p=0.1, inplace=False)
      (classifier): Linear(in_features=768, out_features=12, bias=True)
      (loss_fct): CrossEntropyLoss()
      (crf_layer): Transformer_CRF()
    """
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.num_labels)

        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

        self.use_crf = config.use_crf
        self.crf_layer = Transformer_CRF(config) if self.use_crf else None

        self.use_biaffine = config.use_biaffine
        self.biaffine = BiaffineNER(config) if self.use_biaffine else None

        self.init_weights()

    def forward(self, input_ids, attention_mask=None, token_type_ids=None, position_ids=None, head_mask=None, label_ids=None):
        outputs = self.bert(input_ids,
                            attention_mask=attention_mask,
                            token_type_ids=token_type_ids,
                            position_ids=position_ids,
                            head_mask=head_mask)

        sequence_output = outputs[0]
        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if self.use_crf:
            logits, active_logits, loss = self.crf_layer(logits, label_ids)
        elif self.use_biaffine:
            logits, active_logits, loss = self.biaffine(sequence_output, attention_mask, label_ids)
        else:
            # if attention_mask is not None:
            #     active_idx = attention_mask.view(-1) == 1
            #     active_logits = logits.view(-1, self.num_labels)[active_idx]
            #     active_labels = label_ids.view(-1)[active_idx]
            # else:
            #     active_logits = logits.view(-1, self.num_labels)
            #     active_labels = label_ids.view(-1)
            #
            # # loss_fct = nn.CrossEntropyLoss()
            # loss = self.loss_fct(active_logits, active_labels)
            loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class RobertaNerModel(BertPreTrainedModel):
    config_class = RobertaConfig
    pretrained_model_archive_map = ROBERTA_PRETRAINED_MODEL_ARCHIVE_LIST
    base_model_prefix = "roberta"

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.roberta = RobertaModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.num_labels)

        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

        self.use_crf = config.use_crf
        self.crf_layer = Transformer_CRF(config) if self.use_crf else None

        self.use_biaffine = config.use_biaffine
        self.biaffine = BiaffineNER(config) if self.use_biaffine else None

        self.init_weights()

    def forward(self, input_ids, attention_mask=None, token_type_ids=None, position_ids=None, head_mask=None, label_ids=None):
        """
        :return: raw logits without any softmax or log_softmax transformation

        qoute for reason (https://discuss.pytorch.org/t/logsoftmax-vs-softmax/21386/7):
        You should pass raw logits to nn.CrossEntropyLoss, since the function itself applies F.log_softmax and nn.NLLLoss() on the input.
        If you pass log probabilities (from nn.LogSoftmax) or probabilities (from nn.Softmax()) your loss function won’t work as intended.

        From the pytorch CrossEntropyLoss doc:
        The input is expected to contain raw, unnormalized scores for each class.

        If apply CRF, we cannot use CrossEntropyLoss but instead using NLLLoss ()
        """
        outputs = self.roberta(input_ids,
                               attention_mask=attention_mask,
                               token_type_ids=token_type_ids,
                               position_ids=position_ids,
                               head_mask=head_mask)

        seq_outputs = outputs[0]
        seq_outputs = self.dropout(seq_outputs)
        logits = self.classifier(seq_outputs)

        if self.use_crf:
            logits, active_logits, loss = self.crf_layer(logits, label_ids)
        elif self.use_biaffine:
            logits, active_logits, loss = self.biaffine(seq_outputs, attention_mask, label_ids)
        else:
            # if attention_mask is not None:
            #     active_idx = attention_mask.view(-1) == 1
            #     active_logits = logits.view(-1, self.num_labels)[active_idx]
            #     active_labels = label_ids.view(-1)[active_idx]
            # else:
            #     active_logits = logits.view(-1, self.num_labels)
            #     active_labels = label_ids.view(-1)
            #
            # loss = self.loss_fct(active_logits, active_labels)
            loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class LongformerNerModel(LongformerForTokenClassification):

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.longformer = LongformerModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.num_labels)

        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

        self.use_crf = config.use_crf
        self.crf_layer = Transformer_CRF(config) if self.use_crf else None

        self.use_biaffine = config.use_biaffine
        self.biaffine = BiaffineNER(config) if self.use_biaffine else None

        self.init_weights()

    def forward(self,
                input_ids=None,
                attention_mask=None,
                global_attention_mask=None,
                token_type_ids=None,
                position_ids=None,
                inputs_embeds=None,
                label_ids=None,
                output_attentions=None,
                output_hidden_states=None):
        outputs = self.longformer(input_ids=input_ids,
                                  attention_mask=attention_mask,
                                  global_attention_mask=global_attention_mask,
                                  token_type_ids=token_type_ids,
                                  position_ids=position_ids,
                                  inputs_embeds=inputs_embeds,
                                  output_attentions=output_attentions,
                                  output_hidden_states=output_hidden_states)

        sequence_output = outputs[0]
        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if self.use_crf:
            logits, active_logits, loss = self.crf_layer(logits, label_ids)
        elif self.use_biaffine:
            logits, active_logits, loss = self.biaffine(sequence_output, attention_mask, label_ids)
        else:
            # if attention_mask is not None:
            #     active_idx = attention_mask.view(-1) == 1
            #     active_logits = logits.view(-1, self.num_labels)[active_idx]
            #     active_labels = label_ids.view(-1)[active_idx]
            # else:
            #     active_logits = logits.view(-1, self.num_labels)
            #     active_labels = label_ids.view(-1)
            #
            # # loss_fct = nn.CrossEntropyLoss()
            # loss = self.loss_fct(active_logits, active_labels)
            loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class AlbertNerModel(AlbertPreTrainedModel):
    # config_class = AlbertConfig
    # pretrained_model_archive_map = ALBERT_PRETRAINED_MODEL_ARCHIVE_MAP
    # base_model_prefix = 'albert'

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.albert = AlbertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.num_labels)

        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

        self.use_crf = config.use_crf
        self.crf_layer = Transformer_CRF(config) if self.use_crf else None

        self.use_biaffine = config.use_biaffine
        self.biaffine = BiaffineNER(config) if self.use_biaffine else None

        self.init_weights()

    def forward(self, input_ids, attention_mask=None, token_type_ids=None, position_ids=None, head_mask=None, label_ids=None):
        outputs = self.albert(input_ids,
                              attention_mask=attention_mask,
                              token_type_ids=token_type_ids,
                              position_ids=position_ids,
                              head_mask=head_mask)

        seq_outputs = outputs[0]
        seq_outputs = self.dropout(seq_outputs)
        logits = self.classifier(seq_outputs)

        if self.use_crf:
            logits, active_logits, loss = self.crf_layer(logits, label_ids)
        elif self.use_biaffine:
            logits, active_logits, loss = self.biaffine(seq_outputs, attention_mask, label_ids)
        else:
            # if attention_mask is not None:
            #     active_idx = attention_mask.view(-1) == 1
            #     active_logits = logits.view(-1, self.num_labels)[active_idx]
            #     active_labels = label_ids.view(-1)[active_idx]
            # else:
            #     active_logits = logits.view(-1, self.num_labels)
            #     active_labels = label_ids.view(-1)
            #
            # loss = self.loss_fct(active_logits, active_labels)
            loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class DistilBertNerModel(BertPreTrainedModel):
    config_class = DistilBertConfig
    pretrained_model_archive_map = DISTILBERT_PRETRAINED_MODEL_ARCHIVE_LIST
    base_model_prefix = 'distilbert'

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.distilbert = DistilBertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.num_labels)

        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

        self.use_crf = config.use_crf
        self.crf_layer = Transformer_CRF(config) if self.use_crf else None

        self.use_biaffine = config.use_biaffine
        self.biaffine = BiaffineNER(config) if self.use_biaffine else None

        self.init_weights()

    def forward(self,
                input_ids,
                attention_mask=None,
                token_type_ids=None,
                position_ids=None,
                head_mask=None,
                label_ids=None):

        outputs = self.distilbert(input_ids,
                                  attention_mask=attention_mask,
                                  head_mask=head_mask)

        sequence_output = outputs[0]
        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if self.use_crf:
            logits, active_logits, loss = self.crf_layer(logits, label_ids)
        elif self.use_biaffine:
            logits, active_logits, loss = self.biaffine(sequence_output, attention_mask, label_ids)
        else:
            # if attention_mask is not None:
            #     active_idx = attention_mask.view(-1) == 1
            #     active_logits = logits.view(-1, self.num_labels)[active_idx]
            #     active_labels = label_ids.view(-1)[active_idx]
            # else:
            #     active_logits = logits.view(-1, self.num_labels)
            #     active_labels = label_ids.view(-1)
            #
            # loss = self.loss_fct(active_logits, active_labels)
            loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class XLNetNerModel(XLNetForTokenClassification):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.xlnet = XLNetModel(config)
        self.classifier = nn.Linear(config.d_model, self.num_labels)
        self.dropout = nn.Dropout(config.dropout)
        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()
        if config.use_crf:
            raise Warning("Not support CRF for XLNet for now.")
        if config.use_biaffine:
            raise Warning("Not support biaffine for XLNet for now")
        # will not support biaffine as well
        self.crf_layer = None
        self.biaffine = None
        self.init_weights()

    def forward(self,
                input_ids=None,
                attention_mask=None,
                mems=None,
                perm_mask=None,
                target_mapping=None,
                token_type_ids=None,
                input_mask=None,
                head_mask=None,
                inputs_embeds=None,
                use_cache=True,
                label_ids=None,
                output_attentions=None,
                output_hidden_states=None,
        ):

        outputs = self.xlnet(input_ids=input_ids,
                             attention_mask=attention_mask,
                             mems=mems,
                             perm_mask=perm_mask,
                             target_mapping=target_mapping,
                             token_type_ids=token_type_ids,
                             input_mask=input_mask,
                             head_mask=head_mask,
                             inputs_embeds=inputs_embeds)

        seq_outputs = outputs[0]
        seq_outputs = self.dropout(seq_outputs)
        logits = self.classifier(seq_outputs)

        loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class BartNerModel(PreTrainedModel):
    """
        According to https://arxiv.org/pdf/1910.13461.pdf section 3.2,
        the token classification tasks use the top decoder hidden state.
        We will adopt their implementation only using the decoder (dco) for classification,
        we do provide the option to concat encoder output with decoder output.
    """
    config_class = BartConfig
    base_model_prefix = "bart"
    pretrained_model_archive_map = {"bart-large": "https://s3.amazonaws.com/models.huggingface.co/bert/facebook/bart-large/pytorch_model.bin"}

    def __init__(self, config, output_concat=False):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.bart = BartModel(config)
        self.dropout = nn.Dropout(config.dropout)
        self.classifier = nn.Linear(config.d_model, config.num_labels)

        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

        self.use_crf = config.use_crf
        self.crf_layer = Transformer_CRF(config) if self.use_crf else None

        self.use_biaffine = config.use_biaffine
        self.biaffine = BiaffineNER(config) if self.use_biaffine else None

        self.output_concat = output_concat
        self.init_weights()

    def _init_weights(self, module):
        std = self.config.init_std
        # called init_bert_params in fairseq
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        if isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    def forward(self, input_ids, attention_mask=None, decoder_input_ids=None, encoder_outputs=None, decoder_attention_mask=None, decoder_cached_states=None, label_ids=None):
        # dco = decoder output; eco = encoder output
        dco, eco = self.bart(input_ids,
                             attention_mask=attention_mask,
                             decoder_input_ids=decoder_input_ids,
                             encoder_outputs=encoder_outputs,
                             decoder_attention_mask=decoder_attention_mask,
                             decoder_cached_states=decoder_cached_states
                             )
        if self.output_concat:
            sequence_output = torch.cat((dco, eco), 2)
        else:
            sequence_output = dco

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if self.use_crf:
            logits, active_logits, loss = self.crf_layer(logits, label_ids)
        elif self.use_biaffine:
            logits, active_logits, loss = self.biaffine(sequence_output, attention_mask, label_ids)
        else:
            # if attention_mask is not None:
            #     active_idx = attention_mask.view(-1) == 1
            #     active_logits = logits.view(-1, self.num_labels)[active_idx]
            #     active_labels = label_ids.view(-1)[active_idx]
            # else:
            #     active_logits = logits.view(-1, self.num_labels)
            #     active_labels = label_ids.view(-1)
            #
            # # loss_fct = nn.CrossEntropyLoss()
            # loss = self.loss_fct(active_logits, active_labels)
            loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class ElectraNerModel(ElectraForTokenClassification):
    """
    model architecture:
      (bert): ELECTRA
      (dropout): Dropout(p=0.1, inplace=False)
      (classifier): Linear(in_features=768, out_features=12, bias=True)
      (loss_fct): CrossEntropyLoss()
      (crf_layer): Transformer_CRF()
    """

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.electra = ElectraModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.num_labels)

        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

        self.use_crf = config.use_crf
        self.crf_layer = Transformer_CRF(config) if self.use_crf else None

        self.use_biaffine = config.use_biaffine
        self.biaffine = BiaffineNER(config) if self.use_biaffine else None

        self.init_weights()

    def forward(self,
                input_ids=None,
                attention_mask=None,
                token_type_ids=None,
                position_ids=None,
                head_mask=None,
                inputs_embeds=None,
                label_ids=None,
                output_attentions=None,
                output_hidden_states=None):

        outputs = self.electra(input_ids,
                               attention_mask=attention_mask,
                               token_type_ids=token_type_ids,
                               position_ids=position_ids,
                               inputs_embeds=inputs_embeds,
                               head_mask=head_mask)

        sequence_output = outputs[0]
        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if self.use_crf:
            logits, active_logits, loss = self.crf_layer(logits, label_ids)
        elif self.use_biaffine:
            logits, active_logits, loss = self.biaffine(sequence_output, attention_mask, label_ids)
        else:
            # if attention_mask is not None:
            #     active_idx = attention_mask.view(-1) == 1
            #     active_logits = logits.view(-1, self.num_labels)[active_idx]
            #     active_labels = label_ids.view(-1)[active_idx]
            # else:
            #     active_logits = logits.view(-1, self.num_labels)
            #     active_labels = label_ids.view(-1)
            #
            # loss = self.loss_fct(active_logits, active_labels)
            loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss


class DeBertaNerModel(DebertaPreTrainedModel):
    _keys_to_ignore_on_load_unexpected = [r"pooler"]

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.deberta = DebertaModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, config.num_labels)

        if config.use_focal_loss:
            self.loss_fct = FocalLoss(gamma=config.focal_loss_gamma)
        else:
            self.loss_fct = nn.CrossEntropyLoss()

        self.use_crf = config.use_crf
        self.crf_layer = Transformer_CRF(config) if self.use_crf else None

        self.use_biaffine = config.use_biaffine
        self.biaffine = BiaffineNER(config) if self.use_biaffine else None

        self.init_weights()

    def forward(
            self,
            input_ids=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            inputs_embeds=None,
            label_ids=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None):
        
        outputs = self.deberta(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict
        )

        sequence_output = outputs[0]
        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if self.use_crf:
            logits, active_logits, loss = self.crf_layer(logits, label_ids)
        elif self.use_biaffine:
            logits, active_logits, loss = self.biaffine(sequence_output, attention_mask, label_ids)
        else:
            # if attention_mask is not None:
            #     active_idx = attention_mask.view(-1) == 1
            #     active_logits = logits.view(-1, self.num_labels)[active_idx]
            #     active_labels = label_ids.view(-1)[active_idx]
            # else:
            #     active_logits = logits.view(-1, self.num_labels)
            #     active_labels = label_ids.view(-1)
            #
            # loss = self.loss_fct(active_logits, active_labels)
            loss, active_logits = _calculate_loss(logits, attention_mask, label_ids, self.loss_fct, self.num_labels)

        return logits, active_logits, loss
