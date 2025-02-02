import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import config
from config import USE_CUDA, DEVICE,NUM_CUDA
from transformer_encoder import Trm_Encoder


# 权重初始化，默认xavier
def init_network(model, method='xavier', seed=123):
    for name, w in model.named_parameters():
        # if exclude not in name:
        if 'weight' in name:
            if method == 'xavier':
                nn.init.xavier_normal_(w)
            elif method == 'kaiming':
                nn.init.kaiming_normal_(w)
            else:
                nn.init.normal_(w)
        elif 'bias' in name:
            nn.init.constant_(w, 0)
        else:
            pass

def init_lstm_wt(lstm):
    for names in lstm._all_weights:
        for name in names:
            if name.startswith('weight_'):
                wt = getattr(lstm, name)
                wt.data.uniform_(-config.rand_unif_init_mag, config.rand_unif_init_mag)
            elif name.startswith('bias_'):
                # set forget bias to 1
                bias = getattr(lstm, name)
                n = bias.size(0)
                start, end = n // 4, n // 2
                bias.data.fill_(0.)
                bias.data[start:end].fill_(1.)

def init_linear_wt(linear):
    linear.weight.data.normal_(std=config.trunc_norm_init_std)
    if linear.bias is not None:
        linear.bias.data.normal_(std=config.trunc_norm_init_std)

def init_wt_normal(wt):
    wt.data.normal_(std=config.trunc_norm_init_std)

def init_wt_unif(wt):
    wt.data.uniform_(-config.rand_unif_init_mag, config.rand_unif_init_mag)

class Encoder(nn.Module):
    def __init__(self):
        super(Encoder, self).__init__()
        self.embedding = nn.Embedding(config.vocab_size, config.emb_dim)
        init_wt_normal(self.embedding.weight)
        self.lstm = nn.LSTM(config.emb_dim, config.hidden_dim, num_layers=1,
                            batch_first=True, bidirectional=True)
        init_lstm_wt(self.lstm)
        self.W_h = nn.Linear(config.hidden_dim * 2, config.hidden_dim * 2, bias=False)
        self.feature = nn.Linear(config.hidden_dim * 4, config.hidden_dim * 2)
        self.trm_encode = Trm_Encoder()
        self.S = nn.Sigmoid()
        if config.swish:
            self.sw1 = nn.Sequential(nn.Conv1d(2*config.hidden_dim, 2*config.hidden_dim, kernel_size=1, padding=0), nn.BatchNorm1d(2*config.hidden_dim), nn.ReLU())
            self.sw3 = nn.Sequential(nn.Conv1d(2*config.hidden_dim, 2*config.hidden_dim, kernel_size=1, padding=0), nn.ReLU(), nn.BatchNorm1d(2*config.hidden_dim),
                                     nn.Conv1d(2*config.hidden_dim, 2*config.hidden_dim, kernel_size=3, padding=1), nn.ReLU(), nn.BatchNorm1d(2*config.hidden_dim))
            self.sw33 = nn.Sequential(nn.Conv1d(2*config.hidden_dim, 2*config.hidden_dim, kernel_size=1, padding=0), nn.ReLU(), nn.BatchNorm1d(2*config.hidden_dim),
                                      nn.Conv1d(2*config.hidden_dim, 2*config.hidden_dim, kernel_size=3, padding=1), nn.ReLU(), nn.BatchNorm1d(2*config.hidden_dim),
                                      nn.Conv1d(2*config.hidden_dim, 2*config.hidden_dim, kernel_size=3, padding=1), nn.ReLU(), nn.BatchNorm1d(2*config.hidden_dim))
            self.linear = nn.Sequential(nn.Linear(2*config.hidden_dim, 2*config.hidden_dim), nn.GLU(), nn.Dropout(config.dropout))
            self.filter_linear = nn.Linear(6*config.hidden_dim, 2*config.hidden_dim)
            self.tanh = nn.Tanh()
            self.sigmoid = nn.Sigmoid()

    # seq_lens: 1D tensor 应该降序排列
    def forward(self, input_x, seq_lens):
        embedded = self.embedding(input_x)
        packed = pack_padded_sequence(embedded, seq_lens, batch_first=True)

        output, hidden = self.lstm(packed)  # hidden is tuple([2, batch, hid_dim], [2, batch, hid_dim])
        encoder_outputs, _ = pad_packed_sequence(output, batch_first=True)  # [batch, max(seq_lens), 2*hid_dim]
        encoder_outputs = encoder_outputs.contiguous()  # [batch, max(seq_lens), 2*hid_dim]
        if config.swish:
            encoder_outputs = encoder_outputs.transpose(1,2)
            conv1 = self.sw1(encoder_outputs)
            conv3 = self.sw3(encoder_outputs)
            conv33 = self.sw33(encoder_outputs)
            conv = torch.cat((conv1, conv3, conv33), 1)
            conv = self.filter_linear(conv.transpose(1, 2))

            gate = self.sigmoid(conv)
            encoder_outputs = encoder_outputs.transpose(1, 2) * gate


        trm_outputs, _, _ = self.trm_encode(input_x,seq_lens) #[batch, max(seq_lens), 2*hid_dim]
        encoder_outputs_ = self.feature(torch.cat([encoder_outputs,trm_outputs],2))#[batch, max(seq_lens), 2*hid_dim]
        gate = self.S(encoder_outputs_)
        encoder_outputs = encoder_outputs_ * gate + encoder_outputs * (1 - gate)

        encoder_feature = encoder_outputs.view(-1, 2 * config.hidden_dim)
        encoder_feature = self.W_h(encoder_feature)  # [batch*max(seq_lens), 2*hid_dim]

        return encoder_outputs, encoder_feature, hidden  # [B, max(seq_lens), 2*hid_dim], [B*max(seq_lens), 2*hid_dim], tuple([2, batch, hid_dim], [2, batch, hid_dim])


class ReduceState(nn.Module):
    def __init__(self):
        super(ReduceState, self).__init__()
        self.reduce_h = nn.Linear(config.hidden_dim * 2, config.hidden_dim)
        init_linear_wt(self.reduce_h)
        self.reduce_c = nn.Linear(config.hidden_dim * 2, config.hidden_dim)
        init_linear_wt(self.reduce_c)

    def forward(self, hidden):
        h, c = hidden  # h, c dim = [2, batch, hidden_dim]当lstm为1层时,设置3层，调试过于麻烦

        h_in = h.transpose(0, 1).contiguous().view(-1, config.hidden_dim * 2)  # [batch, hidden_dim*2]
        hidden_reduced_h = F.relu(self.reduce_h(h_in))  # [batch, hidden_dim]
        c_in = c.transpose(0, 1).contiguous().view(-1, config.hidden_dim * 2)
        hidden_reduced_c = F.relu(self.reduce_c(c_in))

        return (hidden_reduced_h.unsqueeze(0), hidden_reduced_c.unsqueeze(0))  # h, c dim = [1, batch, hidden_dim]


class Attention(nn.Module):
    def __init__(self):
        super(Attention, self).__init__()
        # attention
        if config.is_coverage:
            self.W_c = nn.Linear(1, config.hidden_dim * 2, bias=False)
        self.decode_proj = nn.Linear(config.hidden_dim * 2, config.hidden_dim * 2)
        self.v = nn.Linear(config.hidden_dim * 2, 1, bias=False)

    def forward(self, s_t_hat, encoder_outputs, encoder_feature, enc_padding_mask, coverage):
        b, t_k, n = list(encoder_outputs.size())

        dec_fea = self.decode_proj(s_t_hat)  # B x 2*hid_dim
        dec_fea_expanded = dec_fea.unsqueeze(1).expand(b, t_k, n).contiguous()  # B x t_k x 2*hid_dim
        dec_fea_expanded = dec_fea_expanded.view(-1, n)  # B * t_k x 2*hid_dim

        att_features = encoder_feature + dec_fea_expanded  # B * t_k x 2*hidden_dim
        if config.is_coverage:
            coverage_input = coverage.view(-1, 1)  # B * t_k x 1
            coverage_feature = self.W_c(coverage_input)  # B * t_k x 2*hidden_dim
            att_features = att_features + coverage_feature

        e = torch.tanh(att_features)  # B * t_k x 2*hidden_dim
        scores = self.v(e)  # B * t_k x 1
        scores = scores.view(-1, t_k)  # B x t_k

        attn_dist_ = F.softmax(scores, dim=1) * enc_padding_mask  # B x t_k
        normalization_factor = attn_dist_.sum(1, keepdim=True)
        attn_dist = attn_dist_ / normalization_factor

        attn_dist = attn_dist.unsqueeze(1)  # B x 1 x t_k
        c_t = torch.bmm(attn_dist, encoder_outputs)  # B x 1 x n
        c_t = c_t.view(-1, config.hidden_dim * 2)  # B x 2*hidden_dim

        attn_dist = attn_dist.view(-1, t_k)  # B x t_k

        if config.is_coverage:
            coverage = coverage.view(-1, t_k)
            coverage = coverage + attn_dist

        return c_t, attn_dist, coverage


class Decoder(nn.Module):
    def __init__(self):
        super(Decoder, self).__init__()
        self.attention_network = Attention()
        # decoder
        self.embedding = nn.Embedding(config.vocab_size, config.emb_dim)
        init_wt_normal(self.embedding.weight)
        self.x_context = nn.Linear(config.hidden_dim * 2 + config.emb_dim, config.emb_dim)
        self.lstm = nn.LSTM(config.emb_dim, config.hidden_dim, num_layers=1, batch_first=True, bidirectional=False)
        init_lstm_wt(self.lstm)
        if config.pointer_gen:
            self.p_gen_linear = nn.Linear(config.hidden_dim * 4 + config.emb_dim, 1)

        # p_vocab
        self.out1 = nn.Linear(config.hidden_dim * 3, config.hidden_dim)
        self.out2 = nn.Linear(config.hidden_dim, config.vocab_size)
        init_linear_wt(self.out2)


    def forward(self, y_t_1, s_t_1, encoder_outputs, encoder_feature, enc_padding_mask,
                c_t_1, extra_zeros, enc_batch_extend_vocab, coverage, step):

        if not self.training and step == 0:
            h_decoder, c_decoder = s_t_1
            s_t_hat = torch.cat((h_decoder.view(-1, config.hidden_dim),
                                 c_decoder.view(-1, config.hidden_dim)), 1)  # B x 2*hidden_dim
            # attention
            c_t, _, coverage_next = self.attention_network(s_t_hat, encoder_outputs, encoder_feature,
                                                           enc_padding_mask, coverage)
            coverage = coverage_next

        y_t_1_embd = self.embedding(y_t_1)
        x = self.x_context(torch.cat((c_t_1, y_t_1_embd), 1))
        lstm_out, s_t = self.lstm(x.unsqueeze(1), s_t_1)

        h_decoder, c_decoder = s_t
        s_t_hat = torch.cat((h_decoder.view(-1, config.hidden_dim),
                             c_decoder.view(-1, config.hidden_dim)), 1)  # B x 2*hidden_dim
        c_t, attn_dist, coverage_next = self.attention_network(s_t_hat, encoder_outputs, encoder_feature,
                                                               enc_padding_mask, coverage)

        if self.training or step > 0:
            coverage = coverage_next

        p_gen = None
        if config.pointer_gen:

            p_gen_input = torch.cat((c_t, s_t_hat, x), 1)
            # B x (2*2*hidden_dim + emb_dim)
            p_gen = self.p_gen_linear(p_gen_input)
            p_gen = torch.sigmoid(p_gen)

        output = torch.cat((lstm_out.view(-1, config.hidden_dim), c_t), 1)  # B x hidden_dim * 3
        output = self.out1(output)  # B x hidden_dim
        output = self.out2(output)  # B x vocab_size
        vocab_dist = F.softmax(output, dim=1)

        if config.pointer_gen:
            vocab_dist_ = p_gen * vocab_dist
            attn_dist_ = (1 - p_gen) * attn_dist

            if extra_zeros is not None:
                vocab_dist_ = torch.cat([vocab_dist_, extra_zeros], 1)

            final_dist = vocab_dist_.scatter_add(1, enc_batch_extend_vocab, attn_dist_)  # 将词表和当前batch单词结合起来作为最终词表
            """***********************************************2020.1.2调试至此"""
        else:
            final_dist = vocab_dist

        return final_dist, s_t, c_t, attn_dist, p_gen, coverage


class Model(object):
    def __init__(self, model_file_path=None, is_eval=False):
        encoder = Encoder()
        decoder = Decoder()
        reduce_state = ReduceState()
        trm_encoder = Trm_Encoder()


        # decoder与encoder参数共享
        decoder.embedding.weight = encoder.embedding.weight = trm_encoder.embedding.weight
        if is_eval:
            encoder = encoder.eval()
            decoder = decoder.eval()
            reduce_state = reduce_state.eval()
            trm_encoder = Trm_Encoder()

        if USE_CUDA:
            encoder = encoder.to(DEVICE)
            decoder = decoder.to(DEVICE)
            reduce_state = reduce_state.to(DEVICE)
            trm_encoder = trm_encoder.to(DEVICE)



        self.encoder = encoder
        self.decoder = decoder
        self.reduce_state = reduce_state
        self.trm_encoder = trm_encoder

        if model_file_path is not None:
            state = torch.load(model_file_path, map_location=lambda storage, location: storage)
            self.encoder.load_state_dict(state['encoder_state_dict'])
            self.decoder.load_state_dict(state['decoder_state_dict'], strict=False)
            self.reduce_state.load_state_dict(state['reduce_state_dict'])
