import torch
import torch.nn as nn
from random import shuffle
import torch.optim as optim
import torch.nn.functional as F
import torchvision.utils as vutils
from torch.optim import lr_scheduler
from ..datasets import *
from ..utils import *
from .ops import *
from scipy.io import wavfile
import multiprocessing as mp
import numpy as np
import timeit
import random
from random import shuffle
from tensorboardX import SummaryWriter
from .generator import *
from .discriminator import *
from .core import *
import json
import os
from torch import autograd
from scipy import signal
from pase.models.frontend import wf_builder


# custom weights initialization called on netG and netD
def weights_init(m):
    if hasattr(m, 'no_init'):
        print('Found no_init module')
        return
    classname = m.__class__.__name__
    if classname.find('Conv1DResBlock') != -1:
        print('Initializing weights of convresblock to 0.0, 0.02')
        for k, p in m.named_parameters():
            if 'weight' in k and 'conv' in k:
                p.data.normal_(0.0, 0.02)
    elif classname.find('Conv1d') != -1:
        print('Initialzing weight to 0.0, 0.02 for module: ', m)
        m.weight.data.normal_(0.0, 0.02)
        if hasattr(m, 'bias') and m.bias is not None:
            print('bias to 0 for module: ', m)
            m.bias.data.fill_(0)
    elif classname.find('Linear') != -1:
        print('Initializing FC weight to xavier uniform')
        nn.init.xavier_uniform_(m.weight.data)

def wsegan_weights_init(m):
    if hasattr(m, 'no_init'):
        print('Found no_init module')
        return
    classname = m.__class__.__name__
    if classname.find('Conv1DResBlock') != -1:
        print('Initializing weights of convresblock to 0.0, 0.02')
        for k, p in m.named_parameters():
            if 'weight' in k and 'conv' in k:
                nn.init.xavier_uniform_(p.data)
    elif classname.find('Conv1d') != -1:
        print('Initialzing weight to XU for module: ', m)
        nn.init.xavier_uniform_(m.weight.data)
    elif classname.find('ConvTranspose1d') != -1:
        print('Initialzing weight to XU for module: ', m)
        nn.init.xavier_uniform_(m.weight.data)
    elif classname.find('Linear') != -1:
        print('Initializing FC weight to XU')
        nn.init.xavier_uniform_(m.weight.data)

def pasegan_weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv1d') != -1:
        print('Initializing weight to orthogonal for module: ', m)
        nn.init.orthogonal_(m.weight.data)

    if classname.find('ConvTranspose1d') != -1:
        print('Initializing weight to orthogonal for module: ', m)
        nn.init.orthogonal_(m.weight.data)

def z_dropout(m):
    classname = m.__class__.__name__
    if classname.find('Dropout') != -1:
        # let it active
        m.train()
    else:
        m.eval()


class SEGAN(Model):

    def __init__(self, opts, name='SEGAN',
                 generator=None,
                 discriminator=None):
        super(SEGAN, self).__init__(name)
        self.save_path = opts.save_path
        self.preemph = opts.preemph
        self.reg_loss = getattr(F, opts.reg_loss)
        if generator is None:
            self.build_generator(opts)
        else:
            self.G = generator

        if discriminator is None:
            self.build_discriminator(opts)
        else:
            self.D = discriminator

    def build_generator(self, opts):
        self.G = Generator(1,
                           opts.genc_fmaps,
                           opts.gkwidth,
                           opts.genc_poolings,
                           opts.gdec_fmaps,
                           opts.gdec_kwidth,
                           opts.gdec_poolings,
                           z_dim=opts.z_dim,
                           no_z=opts.no_z,
                           skip=(not opts.no_skip),
                           norm_type=opts.gnorm_type,
                           bias=opts.bias,
                           skip_init=opts.skip_init,
                           skip_type=opts.skip_type,
                           skip_merge=opts.skip_merge,
                           skip_kwidth=opts.skip_kwidth,
                           dec_type=opts.gdec_type,
                           num_classes=opts.num_classes)
        self.G.apply(weights_init)

    def build_discriminator(self, opts):
        dkwidth = opts.gkwidth if opts.dkwidth is None else opts.dkwidth
        self.D = Discriminator(2, opts.denc_fmaps, dkwidth,
                               poolings=opts.denc_poolings,
                               pool_type=opts.dpool_type,
                               pool_slen=opts.dpool_slen, 
                               norm_type=opts.dnorm_type,
                               phase_shift=opts.phase_shift,
                               sinc_conv=opts.sinc_conv,
                               num_classes=opts.num_classes)
        self.D.apply(weights_init)


    def generate(self, inwav, z = None, device='cpu'):
        self.G.eval()
        N = 16384
        x = np.zeros((1, 1, N))
        c_res = None
        slice_idx = torch.zeros(1)
        for beg_i in range(0, inwav.shape[2], N):
            if inwav.shape[2] - beg_i < N:
                length = inwav.shape[2] - beg_i
                pad = N - length
            else:
                length = N
                pad = 0
            if pad  > 0:
                x[0, 0] = torch.cat((inwav[0, 0, beg_i:beg_i + length],
                                    torch.zeros(pad).to(device)), dim=0)
            else:
                x[0, 0] = inwav[0, 0, beg_i:beg_i + length]
            #x = torch.FloatTensor(x)
            if isinstance(x, np.ndarray):
                x = torch.FloatTensor(x)
            x = x.to(device)
            canvas_w, hall = self.infer_G(x, z=z, ret_hid=True)
            nums = []
            for k in hall.keys():
                if 'enc' in k and 'zc' not in k:
                    nums.append(int(k.split('_')[1]))
            g_c = hall['enc_{}'.format(max(nums))]
            if z is None and hasattr(self.G, 'z'):
                # if z was created inside G as first inference
                z = self.G.z
            if pad > 0:
                canvas_w = canvas_w[0, 0, :-pad]
            canvas_w = canvas_w.data.cpu().numpy().squeeze()
            if c_res is None:
                c_res = canvas_w
            else:
                c_res = np.concatenate((c_res, canvas_w))
            slice_idx += 1
        # de-emph
        c_res = de_emphasize(c_res, self.preemph)
        return c_res, g_c

    def discriminate(self, cwav, nwav):
        self.D.eval()
        d_in = torch.cat((cwav, nwav), dim=1)
        d_veredict, _ = self.D(d_in)
        return d_veredict

    def infer_G(self, nwav, cwav=None, z=None, ret_hid=False, test=False):
        if test:
            self.G.eval()
        if ret_hid:
            Genh, hall = self.G(nwav, z=z, ret_hid=ret_hid)
            return Genh, hall
        else:
            Genh = self.G(nwav, z=z, ret_hid=ret_hid)
            return Genh

    def infer_D(self, x_, ref):
        D_in = torch.cat((x_, ref), dim=1)
        return self.D(D_in)

    def gen_train_samples(self, clean_samples, noisy_samples, z_sample, 
                          iteration=None):
        if z_sample is not None:
            canvas_w = self.infer_G(noisy_samples, clean_samples, z=z_sample,
                                    test=True)
        else:
            canvas_w = self.infer_G(noisy_samples, clean_samples, test=True)
        sample_dif = noisy_samples - clean_samples
        # sample wavs
        for m in range(noisy_samples.size(0)):
            m_canvas = de_emphasize(canvas_w[m,
                                             0].cpu().data.numpy(),
                                    self.preemph)
            print('w{} max: {:.3f} min: {:.3f}'.format(m,
                                               m_canvas.max(),
                                               m_canvas.min()))
            wavfile.write(os.path.join(self.save_path,
                                       'sample_{}-'
                                       '{}.wav'.format(iteration,
                                                       m)),
                          int(16e3), m_canvas)
            m_clean = de_emphasize(clean_samples[m,
                                                 0].cpu().data.numpy(),
                                   self.preemph)
            m_noisy = de_emphasize(noisy_samples[m,
                                                 0].cpu().data.numpy(),
                                   self.preemph)
            m_dif = de_emphasize(sample_dif[m,
                                            0].cpu().data.numpy(),
                                 self.preemph)
            m_gtruth_path = os.path.join(self.save_path,
                                         'gtruth_{}.wav'.format(m))
            if not os.path.exists(m_gtruth_path):
                wavfile.write(os.path.join(self.save_path,
                                           'gtruth_{}.wav'.format(m)),
                              int(16e3), m_clean)
                wavfile.write(os.path.join(self.save_path,
                                           'noisy_{}.wav'.format(m)),
                              int(16e3), m_noisy)
                wavfile.write(os.path.join(self.save_path,
                                           'dif_{}.wav'.format(m)),
                              int(16e3), m_dif)

    def build_optimizers(self, opts):
        if opts.opt == 'rmsprop':
            Gopt = optim.RMSprop(self.G.parameters(), lr=opts.g_lr)
            Dopt = optim.RMSprop(self.D.parameters(), lr=opts.d_lr)
        elif opts.opt == 'adam':
            Gopt = optim.Adam(self.G.parameters(), lr=opts.g_lr, betas=(0, 0.9))
            Dopt = optim.Adam(self.D.parameters(), lr=opts.d_lr, betas=(0, 0.9))
        else:
            raise ValueError('Unrecognized optimizer {}'.format(opts.opt))
        return Gopt, Dopt

    def train(self, opts, dloader, criterion, l1_init, l1_dec_step,
              l1_dec_epoch, log_freq, va_dloader=None,
              device='cpu'):
        """ Train the SEGAN """

        # create writer
        self.writer = SummaryWriter(os.path.join(self.save_path, 'train'))

        # Build the optimizers
        Gopt, Dopt = self.build_optimizers(opts)

        # attach opts to models so that they are saved altogether in ckpts
        self.G.optim = Gopt
        self.D.optim = Dopt
        
        # Build savers for end of epoch, storing up to 3 epochs each
        eoe_g_saver = Saver(self.G, opts.save_path, max_ckpts=3,
                            optimizer=self.G.optim, prefix='EOE_G-')
        eoe_d_saver = Saver(self.D, opts.save_path, max_ckpts=3,
                            optimizer=self.D.optim, prefix='EOE_D-')
        num_batches = len(dloader) 
        l1_weight = l1_init
        iteration = 1
        timings = []
        evals = {}
        noisy_evals = {}
        noisy_samples = None
        clean_samples = None
        z_sample = None
        patience = opts.patience
        best_val_obj = 0
        # acumulator for exponential avg of valid curve
        acum_val_obj = 0
        # make label tensor
        label = torch.ones(opts.batch_size)
        label = label.to(device)

        for epoch in range(1, opts.epoch + 1):
            beg_t = timeit.default_timer()
            self.G.train()
            self.D.train()
            for bidx, batch in enumerate(dloader, start=1):
                if epoch >= l1_dec_epoch:
                    if l1_weight > 0:
                        l1_weight -= l1_dec_step
                        # ensure it is 0 if it goes < 0
                        l1_weight = max(0, l1_weight)
                sample = batch
                if len(sample) == 4:
                    uttname, clean, noisy, slice_idx = batch
                else:
                    raise ValueError('Returned {} elements per '
                                     'sample?'.format(len(sample)))
                clean = clean.unsqueeze(1)
                noisy = noisy.unsqueeze(1)
                label.resize_(clean.size(0)).fill_(1)
                clean = clean.to(device)
                noisy = noisy.to(device)
                if noisy_samples is None:
                    noisy_samples = noisy[:20, :, :].contiguous()
                    clean_samples = clean[:20, :, :].contiguous()
                # (1) D real update
                Dopt.zero_grad()
                total_d_fake_loss = 0
                total_d_real_loss = 0
                Genh = self.infer_G(noisy, clean)
                lab = label
                d_real, _ = self.infer_D(clean, noisy)
                d_real_loss = criterion(d_real.view(-1), lab)
                d_real_loss.backward()
                total_d_real_loss += d_real_loss
                
                # (2) D fake update
                d_fake, _ = self.infer_D(Genh.detach(), noisy)
                lab = label.fill_(0)
                d_fake_loss = criterion(d_fake.view(-1), lab)
                d_fake_loss.backward()
                total_d_fake_loss += d_fake_loss
                Dopt.step()

                d_loss = d_fake_loss + d_real_loss 

                # (3) G real update
                Gopt.zero_grad()
                lab = label.fill_(1)
                d_fake_, _ = self.infer_D(Genh, noisy)
                g_adv_loss = criterion(d_fake_.view(-1), lab)
                #g_l1_loss = l1_weight * F.l1_loss(Genh, clean)
                g_l1_loss = l1_weight * self.reg_loss(Genh, clean)
                g_loss = g_adv_loss + g_l1_loss
                g_loss.backward()
                Gopt.step()
                end_t = timeit.default_timer()
                timings.append(end_t - beg_t)
                beg_t = timeit.default_timer()
                if z_sample is None and not self.G.no_z:
                    # capture sample now that we know shape after first
                    # inference
                    z_sample = self.G.z[:20, :, :].contiguous()
                    print('z_sample size: ', z_sample.size())
                    z_sample = z_sample.to(device)
                if bidx % log_freq == 0 or bidx >= len(dloader):
                    d_real_loss_v = d_real_loss.cpu().item()
                    d_fake_loss_v = d_fake_loss.cpu().item()
                    g_adv_loss_v = g_adv_loss.cpu().item()
                    g_l1_loss_v = g_l1_loss.cpu().item()
                    log = '(Iter {}) Batch {}/{} (Epoch {}) d_real:{:.4f}, ' \
                          'd_fake:{:.4f}, '.format(iteration, bidx,
                                                   len(dloader), epoch,
                                                   d_real_loss_v,
                                                   d_fake_loss_v)
                    log += 'g_adv:{:.4f}, g_l1:{:.4f} ' \
                           'l1_w: {:.2f}, '\
                           'btime: {:.4f} s, mbtime: {:.4f} s' \
                           ''.format(g_adv_loss_v,
                                     g_l1_loss_v,
                                     l1_weight, 
                                     timings[-1],
                                     np.mean(timings))
                    print(log)
                    self.writer.add_scalar('D_real', d_real_loss_v,
                                           iteration)
                    self.writer.add_scalar('D_fake', d_fake_loss_v,
                                           iteration)
                    self.writer.add_scalar('G_adv', g_adv_loss_v,
                                           iteration)
                    self.writer.add_scalar('G_l1', g_l1_loss_v,
                                           iteration)
                    self.writer.add_histogram('D_fake__hist', d_fake_.cpu().data,
                                              iteration, bins='sturges')
                    self.writer.add_histogram('D_fake_hist', d_fake.cpu().data,
                                              iteration, bins='sturges')
                    self.writer.add_histogram('D_real_hist', d_real.cpu().data,
                                              iteration, bins='sturges')
                    self.writer.add_histogram('Gz', Genh.cpu().data,
                                              iteration, bins='sturges')
                    self.writer.add_histogram('clean', clean.cpu().data,
                                              iteration, bins='sturges')
                    self.writer.add_histogram('noisy', noisy.cpu().data,
                                              iteration, bins='sturges')
                    # get D and G weights and plot their norms by layer and
                    # global
                    def model_weights_norm(model, total_name):
                        total_GW_norm = 0
                        for k, v in model.named_parameters():
                            if 'weight' in k:
                                W = v.data
                                W_norm = torch.norm(W)
                                self.writer.add_scalar('{}_Wnorm'.format(k),
                                                       W_norm,
                                                       iteration)
                                total_GW_norm += W_norm
                        self.writer.add_scalar('{}_Wnorm'.format(total_name),
                                               total_GW_norm,
                                               iteration)
                    model_weights_norm(self.G, 'Gtotal')
                    model_weights_norm(self.D, 'Dtotal')
                    if not opts.no_train_gen:
                        #canvas_w = self.G(noisy_samples, z=z_sample)
                        self.gen_train_samples(clean_samples, noisy_samples,
                                               z_sample,
                                               iteration=iteration)
                iteration += 1

            if va_dloader is not None:
                if len(noisy_evals) == 0:
                    evals_, noisy_evals_ = self.evaluate(opts, va_dloader, 
                                                         log_freq, do_noisy=True)
                    for k, v in noisy_evals_.items():
                        if k not in noisy_evals:
                            noisy_evals[k] = []
                        noisy_evals[k] += v
                        self.writer.add_scalar('noisy-{}'.format(k), 
                                               noisy_evals[k][-1], epoch)
                else:
                    evals_ = self.evaluate(opts, va_dloader, 
                                           log_freq, do_noisy=False)
                for k, v in evals_.items():
                    if k not in evals:
                        evals[k] = []
                    evals[k] += v
                    self.writer.add_scalar('Genh-{}'.format(k), 
                                           evals[k][-1], epoch)
                val_obj = evals['covl'][-1] + evals['pesq'][-1] + \
                        evals['ssnr'][-1]
                self.writer.add_scalar('Genh-val_obj',
                                       val_obj, epoch)
                if val_obj > best_val_obj:
                    print('Val obj (COVL + SSNR + PESQ) improved '
                          '{} -> {}'.format(best_val_obj,
                                            val_obj))
                    best_val_obj = val_obj
                    patience = opts.patience
                    # save models with true valid curve is minimum
                    self.G.save(self.save_path, iteration, True)
                    self.D.save(self.save_path, iteration, True)
                else:
                    patience -= 1
                    print('Val loss did not improve. Patience'
                          '{}/{}'.format(patience,
                                         opts.patience))
                    if patience <= 0:
                        print('STOPPING SEGAN TRAIN: OUT OF PATIENCE.')
                        break

            # save models in end of epoch with EOE savers
            self.G.save(self.save_path, iteration, saver=eoe_g_saver)
            self.D.save(self.save_path, iteration, saver=eoe_d_saver)


    def evaluate(self, opts, dloader, log_freq, do_noisy=False,
                 max_samples=1, device='cpu'):
        """ Objective evaluation with PESQ, SSNR, COVL, CBAK and CSIG """
        self.G.eval()
        self.D.eval()
        evals = {'pesq':[], 'ssnr':[], 'csig':[],
                 'cbak':[], 'covl':[]}
        pesqs = []
        ssnrs = []
        if do_noisy:
            noisy_evals = {'pesq':[], 'ssnr':[], 'csig':[],
                           'cbak':[], 'covl':[]}
            npesqs = []
            nssnrs = []
        if not hasattr(self, 'pool'):
            self.pool = mp.Pool(opts.eval_workers)
        total_s = 0
        timings = []
        with torch.no_grad():
            # going over dataset ONCE
            for bidx, batch in enumerate(dloader, start=1):
                sample = batch
                if len(sample) == 4:
                    uttname, clean, noisy, slice_idx = batch
                else:
                    raise ValueError('Returned {} elements per '
                                     'sample?'.format(len(sample)))
                clean = clean
                noisy = noisy.unsqueeze(1)
                clean = clean.to(device)
                noisy = noisy.to(device)
                Genh = self.infer_G(noisy).squeeze(1)
                clean_npy = clean.cpu().data.numpy()
                Genh_npy = Genh.cpu().data.numpy()
                clean_npy = np.apply_along_axis(de_emphasize, 0, clean_npy,
                                                self.preemph)
                Genh_npy = np.apply_along_axis(de_emphasize, 0, Genh_npy,
                                                self.preemph)
                beg_t = timeit.default_timer()
                if do_noisy:
                    noisy_npy = noisy.cpu().data.numpy()
                    noisy_npy = np.apply_along_axis(de_emphasize, 0, noisy_npy,
                                                    self.preemph)
                    args = [(clean_npy[i], Genh_npy[i], noisy_npy[i]) for i in \
                            range(clean.size(0))]
                else:
                    args = [(clean_npy[i], Genh_npy[i], None) for i in \
                            range(clean.size(0))]
                map_ret = self.pool.map(composite_helper, args)
                end_t = timeit.default_timer()
                print('Time to process eval with {} samples ' \
                      ': {} s'.format(clean.size(0), end_t - beg_t))
                if bidx >= max_samples:
                    break

            def fill_ret_dict(ret_dict, in_dict):
                for k, v in in_dict.items():
                    ret_dict[k].append(v)

            if do_noisy:
                for eval_, noisy_eval_ in map_ret:
                    fill_ret_dict(evals, eval_)
                    fill_ret_dict(noisy_evals, noisy_eval_)
                return evals, noisy_evals
            else:
                for eval_ in map_ret:
                    fill_ret_dict(evals, eval_)
                return evals

class WSEGAN(SEGAN):

    def __init__(self, opts, name='WSEGAN',
                 generator=None,
                 discriminator=None):
        self.lbd = 1
        self.critic_iters = 1
        self.misalign_pair = opts.misalign_pair
        self.interf_pair = opts.interf_pair
        self.pow_weight = opts.pow_weight
        self.fe_weight = opts.fe_weight
        self.gan_loss = opts.gan_loss
        self.n_fft = opts.n_fft
        super(WSEGAN, self).__init__(opts, name, 
                                     generator=generator,
                                     discriminator=discriminator)

    def build_generator(self, opts):
        self.G = Generator(1,
                           opts.genc_fmaps,
                           opts.gkwidth,
                           opts.genc_poolings,
                           opts.gdec_fmaps,
                           opts.gdec_kwidth,
                           opts.gdec_poolings,
                           z_dim=opts.z_dim,
                           no_z=opts.no_z,
                           skip=(not opts.no_skip),
                           norm_type=opts.gnorm_type,
                           bias=opts.bias,
                           skip_init=opts.skip_init,
                           skip_type=opts.skip_type,
                           skip_merge=opts.skip_merge,
                           skip_kwidth=opts.skip_kwidth,
                           dec_type=opts.gdec_type,
                           z_hypercond=opts.z_hypercond,
                           skip_hypercond=opts.skip_hypercond,
                           #num_classes=opts.num_classes,
                           cond_dim=opts.cond_dim,
                           condkwidth=opts.condkwidth)
        if opts.g_ortho > 0:
            self.G.apply(pasegan_weights_init)
        else:
            self.G.apply(wsegan_weights_init)

    def build_gema(self, opts, device):
        if opts.gema:
            # make a G EMA
            G_ema = Generator(1, 
                              opts.genc_fmaps,
                              opts.gkwidth,
                              opts.genc_poolings,
                              opts.gdec_fmaps,
                              opts.gdec_kwidth,
                              opts.gdec_poolings,
                              z_dim=opts.z_dim,
                              no_z=opts.no_z,
                              skip=(not opts.no_skip),
                              norm_type=opts.gnorm_type,
                              bias=opts.bias,
                              skip_init=opts.skip_init,
                              skip_type=opts.skip_type,
                              skip_merge=opts.skip_merge,
                              skip_kwidth=opts.skip_kwidth,
                              dec_type=opts.gdec_type,
                              z_hypercond=opts.z_hypercond,
                              skip_hypercond=opts.skip_hypercond,
                              cond_dim=opts.cond_dim,
                              condkwidth=opts.condkwidth).to(device)
            self.G_ema = G_ema
            self.ema = ema(self.G, self.G_ema, opts.ema_decay,
                           opts.ema_start)

    def build_discriminator(self, opts):
        dkwidth = opts.gkwidth if opts.dkwidth is None else opts.dkwidth
        self.D = Discriminator(2, opts.denc_fmaps, dkwidth,
                               poolings=opts.denc_poolings,
                               pool_type=opts.dpool_type,
                               pool_slen=opts.dpool_slen, 
                               norm_type=opts.dnorm_type,
                               partial_snorm=opts.partial_snorm,
                               phase_shift=opts.phase_shift,
                               sinc_conv=opts.sinc_conv)
                               #num_classes=opts.num_classes)
        self.D.apply(wsegan_weights_init)

    #def sample_dloader(self, iterator, dloader, device='cpu'):
    def sample_dloader(self, batch, device='cpu'):
        if len(batch) == 2:
            clean, noisy = batch
            slice_idx = 0
            uttname = ''
        elif len(batch) == 3:
            uttname, clean, noisy = batch
            slice_idx = 0
        else:
            uttname, clean, noisy, slice_idx = batch
            slice_idx = slice_idx.to(device)
        clean = clean.unsqueeze(1)
        noisy = noisy.unsqueeze(1)
        clean = clean.to(device)
        noisy = noisy.to(device)
        return uttname, clean, noisy, slice_idx

    def infer_G(self, nwav, cwav=None, lab=None, z=None, ret_hid=False,
                test=False):
        if hasattr(self, 'G_ema') and test:
            self.G_ema.eval()
            Genh = self.G_ema(nwav, z=z, lab=lab,
                              ret_hid=ret_hid)
            self.G_ema.train()
        else:
            if test:
                self.G.eval()
            Genh = self.G(nwav, z=z, lab=lab, ret_hid=ret_hid)
            self.G.train()
        return Genh

    def utt2spkid(self, uttnames, spk2idx):
        spkids = []
        for uttname in uttnames:
            spkid = os.path.basename(uttname).split('_')[0]
            idx = spk2idx[spkid]
            spkids.append(idx)
        return torch.LongTensor(spkids)

    def train(self, opts, dloader, criterion, l1_init, l1_dec_step,
              l1_dec_epoch, log_freq, tr_samples=None, 
              va_dloader=None, frontend=None, 
              device='cpu'):

        """ Train the SEGAN """
        CUDA = device == 'cuda'
        self.num_devices = opts.num_devices

        # create writer to log out losses and stuff
        self.writer = SummaryWriter(os.path.join(opts.save_path, 'train'))

        # Build the optimizers
        Gopt, Dopt = self.build_optimizers(opts)

        # attach opts to models so that they are saved altogether in ckpts
        self.G.optim = Gopt
        self.D.optim = Dopt
        
        # Build savers for end of epoch, storing up to 3 epochs each
        eoe_g_saver = Saver(self.G, opts.save_path, max_ckpts=3,
                            optimizer=self.G.optim, prefix='EOE_G-')
        eoe_d_saver = Saver(self.D, opts.save_path, max_ckpts=3,
                            optimizer=self.D.optim, prefix='EOE_D-')
        self.G.saver = eoe_g_saver
        self.D.saver = eoe_d_saver
        # compute batches per epoch to iterate correctly through enough chunks
        #self.G.load(opts.save_path)
        #self.D.load(opts.save_path)
        # depending on slice_size
        bpe = (tr_samples // opts.slice_size) // opts.batch_size if tr_samples is not None else len(dloader)
        num_batches = len(dloader) 
        l1_weight = l1_init
        if hasattr(opts, 'batch_D'):
            batch_D = opts.batch_D
        else:
            batch_D = False
        iteration = 1
        timings = []
        evals = {}
        noisy_evals = {}
        noisy_samples = None
        clean_samples = None
        z_sample = None
        lab = None
        patience = opts.patience
        best_val_obj = np.inf
        
        # parallelize G and D?
        if self.num_devices > 1 and CUDA:
            G = nn.DataParallel(self.G)
            D = nn.DataParallel(self.D)
        else:
            G = self.G
            D = self.D
        G.to(device)
        D.to(device)
        if opts.gema:
            self.build_gema(opts, device)
            eoe_gema_saver = Saver(self.G_ema, opts.save_path, max_ckpts=3,
                                   optimizer=None,
                                   prefix='EOE_GEMA-')
            self.G_ema.saver = eoe_gema_saver
        #iterator = iter(dloader)
        #for iteration in range(1, opts.epoch * bpe + 1):

        for epoch in range(1, opts.epoch + 1):
            beg_t = timeit.default_timer()
            for batch in dloader:
                lab, clean, noisy, slice_idx = self.sample_dloader(batch,
                                                                   device)
                #lab, clean, noisy, slice_idx = self.sample_dloader(iterator, 
                #                                                   dloader,
                #                                                   device)
                bsz = clean.size(0)
                # First batch will return all hidden feats with ret_hid flag
                if noisy_samples is None:
                    Genh, Ghall = self.infer_G(noisy, z=None, lab=None,
                                               ret_hid=True)
                else:
                    Genh = self.infer_G(noisy, z=None, lab=None, ret_hid=False)
                fake = Genh.detach()
                if hasattr(self.D, 'num_classes') and self.D.num_classes is not None:
                    lab = lab.to(device)
                    fk_lab = self.D.num_classes * torch.ones(bsz).long().to(device)
                    rl_lab = lab
                    cost = F.cross_entropy
                else:
                    rl_lab = torch.ones(bsz).cuda()
                    if self.gan_loss == 'bcgan':
                        fk_lab = torch.zeros(bsz).cuda()
                        cost = F.binary_cross_entropy_with_logits
                    elif self.gan_loss == 'lsgan':
                        fk_lab = -1 * torch.ones(bsz).cuda()
                        cost = F.mse_loss
                    elif self.gan_loss == 'hinge':
                        pass
                    else:
                        raise TypeError('Unrecognized gan loss: ',
                                        self.gan_loss)
                D_in_rl = torch.cat((clean, noisy), dim=1)
                D_in_fk = torch.cat((fake, noisy), dim=1)
                if batch_D:
                    # Forward both D batches jointly and then split them
                    # again, to gather computational resources and accelerate
                    D_in = torch.cat((D_in_rl, D_in_fk), dim=0)
                    d_both, _ = D(D_in)
                    d_real, d_fake = torch.chunk(d_both, 2, dim=0)
                else:
                    # FORWARD D real
                    d_real, _ = D(D_in_rl)
                    # FORWARD D fake
                    d_fake, _ = D(D_in_fk)

                if self.gan_loss == 'bcgan' or \
                   self.gan_loss == 'lsgan':
                    # D real loss
                    d_real_loss = cost(d_real, rl_lab)
                    # D fake loss
                    d_fake_loss = cost(d_fake, fk_lab)
                else:
                    d_real_loss = F.relu(1. - d_real).mean()
                    d_fake_loss = F.relu(1. + d_fake).mean()

                d_weight = 0.5 # count only d_fake and d_real
                d_loss = d_fake_loss + d_real_loss

                if self.misalign_pair:
                    clean_shuf = list(torch.chunk(clean, clean.size(0), dim=0))
                    shuffle(clean_shuf)
                    clean_shuf = torch.cat(clean_shuf, dim=0)
                    #d_fake_shuf, _ = self.infer_D(clean, clean_shuf)
                    D_shuf_in = torch.cat((clean, clean_shuf), dim=1)
                    d_fake_shuf, _ = D(D_shuf_in)
                    d_fake_shuf_loss = cost(d_fake_shuf, fk_lab)
                    d_weight = 1 / 3 # count 3 components now
                    d_loss += d_fake_shuf_loss

                if self.interf_pair:
                    # put interferring squared signals with random amplitude and
                    # freq as fake signals mixed with clean data
                    # TODO: Beware with hard-coded values! possibly improve this
                    freqs = [250, 1000, 4000]
                    amps = [0.01, 0.05, 0.1, 1]
                    bsz = clean.size(0)
                    squares = []
                    t = np.linspace(0, 2, 32000)
                    for _ in range(bsz):
                        f_ = random.choice(freqs)
                        a_ = random.choice(amps)
                        sq = a_ * signal.square(2 * np.pi * f_ * t)
                        sq = sq[:clean.size(-1)].reshape((1, -1))
                        squares.append(torch.FloatTensor(sq))
                    squares = torch.cat(squares, dim=0).unsqueeze(1)
                    if clean.is_cuda:
                        squares = squares.to('cuda')
                    interf = clean + squares
                    D_fake_inter_in = torch.cat((interf, noisy), dim=1)
                    d_fake_inter, _ = D(D_fake_inter_in)
                    #d_fake_inter, _ = self.infer_D(interf, noisy)
                    d_fake_inter_loss = cost(d_fake_inter, fk_lab)
                    d_weight = 1 / 4 # count 4 components in d loss now
                    d_loss += d_fake_inter_loss

                #d_loss = d_weight * d_loss
                Dopt.zero_grad()
                d_loss.backward()
                Dopt.step()

                D_fake__in = torch.cat((Genh, noisy), dim=1)
                d_fake_, _ = D(D_fake__in)
                #d_fake_, _ = self.infer_D(Genh, noisy)

                if hasattr(self.D, 'num_classes') and self.D.num_classes is not None:
                    fk__lab = rl_lab
                else:
                    if self.gan_loss == 'bcgan':
                        fk__lab = torch.ones(d_fake_.size()).cuda()
                    elif self.gan_loss == 'lsgan':
                        # satisfies b - c = 1, and b - a = 2 (LSGAN paper)
                        # being b ~ D(x), a ~ D(G(z)) and c ~ D(G(z))_real
                        fk__lab = torch.zeros(d_fake.size()).cuda()

                if self.gan_loss == 'bcgan' or \
                   self.gan_loss == 'lsgan':
                    g_adv_loss = cost(d_fake_,  fk__lab)
                else:
                    g_adv_loss = -d_fake_.mean()
                # POWER Loss -----------------------------------
                if self.pow_weight > 0:
                    # make stft of gtruth
                    clean_stft = torch.stft(clean.squeeze(1), 
                                            n_fft=min(clean.size(-1), self.n_fft), 
                                            hop_length=160,
                                            win_length=320,
                                            normalized=True)
                    clean_mod = torch.norm(clean_stft, 2, dim=3)
                    clean_mod_pow = 10 * torch.log10(clean_mod ** 2 + 10e-20)
                    Genh_stft = torch.stft(Genh.squeeze(1), 
                                           n_fft=min(Genh.size(-1), self.n_fft),
                                           hop_length=160, 
                                           win_length=320, normalized=True)
                    Genh_mod = torch.norm(Genh_stft, 2, dim=3)
                    Genh_mod_pow = 10 * torch.log10(Genh_mod ** 2 + 10e-20)
                    pow_loss = self.pow_weight * F.l1_loss(Genh_mod_pow, clean_mod_pow)
                else:
                    pow_loss = torch.zeros(1).to(device)
                    clean_mod_pow = Genh_mod_pow = None
                if frontend is not None:
                    # merge real and G(z, c) into one large batch
                    fe_input = make_divN(torch.cat((clean, Genh),
                                                    dim=0).transpose(1, 2),
                                         160, 'reflect').transpose(1, 2)
                    assert not frontend.training
                    fe_h = frontend(fe_input)
                    # split batch again to compute diffs
                    fe_clean, fe_Genh = torch.chunk(fe_h, 2, dim=0)
                    fe_loss = self.fe_weight * F.l1_loss(fe_Genh, fe_clean)
                else:
                    fe_loss = torch.zeros(1).to(device)

                G_cost = g_adv_loss + fe_loss + pow_loss
                Gopt.zero_grad()
                G_cost.backward()
                if opts.g_ortho > 0.0:
                    # orthogonal regularization in G
                    ortho_(self.G, opts.g_ortho, [])
                Gopt.step()
                if hasattr(self, 'ema'):
                    # update the EMA updates
                    self.ema.update(iteration)
                end_t = timeit.default_timer()
                timings.append(end_t - beg_t)
                beg_t = timeit.default_timer()
                if noisy_samples is None:
                    noisy_samples = noisy[:20, :, :].contiguous()
                    clean_samples = clean[:20, :, :].contiguous()
                if z_sample is None and not self.G.no_z:
                    # capture sample now that we know shape after first
                    # inference
                    z_sample = Ghall['z'][:20, :, :].contiguous()
                    z_sample = z_sample.to(device)
                    # concat some zero samples too (center of pdf)
                    z_sample = torch.cat((z_sample,
                                          torch.zeros(z_sample.shape).to(device)),
                                         axis=0)
                if iteration % log_freq == 0:
                    log = 'Iter {}/{} ({} bpe) d_loss:{:.4f} (d_real_loss: {:.4f}, ' \
                          'd_fake_loss: {:.4f}), ' \
                          'g_loss: {:.4f} (g_adv_loss: {:.4f}, pow_loss: {:.4f}), ' \
                          'fe_loss: {:.4f} ' \
                          ''.format(iteration,
                                    bpe * opts.epoch,
                                    bpe,
                                    d_loss.item(),
                                    d_real_loss.item(),
                                    d_fake_loss.item(),
                                    G_cost.item(),
                                    g_adv_loss.item(),
                                    pow_loss.item(),
                                    fe_loss.item())

                    log += 'btime: {:.4f} s, mbtime: {:.4f} s' \
                           ''.format(timings[-1],
                                     np.mean(timings))
                    print(log)
                    self.writer.add_scalar('D_loss', d_loss.item(),
                                           iteration)
                    self.writer.add_scalar('D_real_loss', d_real_loss.item(),
                                           iteration)
                    self.writer.add_scalar('D_fake_loss', d_fake_loss.item(),
                                           iteration)
                    self.writer.add_scalar('G_loss', G_cost.item(),
                                           iteration)
                    self.writer.add_scalar('G_adv_loss', g_adv_loss.item(),
                                           iteration)
                    self.writer.add_scalar('G_pow_loss', pow_loss.item(),
                                           iteration)
                    self.writer.add_scalar('G_fe_loss', fe_loss.item(),
                                           iteration)
                    if clean_mod_pow is not None:
                        self.writer.add_histogram('clean_mod_pow',
                                                  clean_mod_pow.cpu().data,
                                                  iteration,
                                                  bins='sturges')
                        self.writer.add_histogram('Genh_mod_pow',
                                                  Genh_mod_pow.cpu().data,
                                                  iteration,
                                                  bins='sturges')
                    self.writer.add_histogram('Gz', Genh.cpu().data,
                                              iteration, bins='sturges')
                    self.writer.add_histogram('clean', clean.cpu().data,
                                              iteration, bins='sturges')
                    self.writer.add_histogram('noisy', noisy.cpu().data,
                                              iteration, bins='sturges')
                    if hasattr(self.G, 'skips'):
                        for skip_id, skip in enumerate(self.G.skips):
                            if skip.skip_type == 'alpha':
                                self.writer.add_histogram('skip_alpha_{}'.format(skip_id),
                                                          skip.skip_k.data,
                                                          iteration, 
                                                          bins='sturges')
                            elif skip.skip_type == 'sconv':
                                self.writer.add_histogram('skip_sconv_{}'.format(skip_id),
                                                          skip.skip_k.conv.weight.data,
                                                          iteration, 
                                                          bins='sturges')
                    # get D and G weights and plot their norms by layer and global
                    def model_weights_norm(model, total_name):
                        total_GW_norm = 0
                        for k, v in model.named_parameters():
                            if 'weight' in k:
                                W = v.data
                                W_norm = torch.norm(W)
                                self.writer.add_scalar('{}_Wnorm'.format(k),
                                                       W_norm,
                                                       iteration)
                                total_GW_norm += W_norm
                        self.writer.add_scalar('{}_Wnorm'.format(total_name),
                                               total_GW_norm,
                                               iteration)
                    model_weights_norm(self.G, 'Gtotal')
                    model_weights_norm(self.D, 'Dtotal')
                    if hasattr(self, 'G_ema'):
                        model_weights_norm(self.G_ema, 'Gematotal')
                    if not opts.no_train_gen:
                        self.gen_train_samples(clean_samples, noisy_samples,
                                               z_sample,
                                               iteration=iteration)
                    # BEWARE: There is no evaluation in Whisper SEGAN (WSEGAN)
                    # TODO: Perhaps add some MCD/F0 RMSE metric
                if iteration % bpe == 0:
                    # save models in end of epoch with EOE savers
                    self.G.save(self.save_path, iteration, saver=eoe_g_saver)
                    self.D.save(self.save_path, iteration, saver=eoe_d_saver)
                    if hasattr(self, 'G_ema'):
                        self.G_ema.save(self.save_path, iteration,
                                        saver=eoe_gema_saver)

                iteration += 1

    def generate(self, inwav, z = None):
        # simplified inference without chunking
        if hasattr(self, 'G_ema'):
            G = self.G_ema
        else:
            G = self.G
        G.eval()
        ori_len = inwav.size(2)
        p_wav = make_divN(inwav.transpose(1, 2), 1024).transpose(1, 2)
        c_res, hall = self.infer_G(p_wav, z=z, ret_hid=True)
        c_res = c_res[0, 0, :ori_len].cpu().data.numpy()
        c_res = de_emphasize(c_res, self.preemph)
        return c_res, hall


class GSEGAN(WSEGAN):

    def __init__(self, opts, 
                 name='GSEGAN'):
        super().__init__(opts, name=name)
        self.disable_aco = opts.disable_aco

    def build_generator(self, opts):
        self.G = Generator(1,
                           opts.genc_fmaps,
                           opts.gkwidth,
                           opts.genc_poolings,
                           opts.gdec_fmaps,
                           opts.gdec_kwidth,
                           opts.gdec_poolings,
                           z_dim=opts.z_dim,
                           no_z=opts.no_z,
                           skip=(not opts.no_skip),
                           norm_type=opts.gnorm_type,
                           bias=opts.bias,
                           skip_init=opts.skip_init,
                           skip_type=opts.skip_type,
                           skip_merge=opts.skip_merge,
                           skip_kwidth=opts.skip_kwidth,
                           dec_type=opts.gdec_type,
                           cond_dim=opts.cond_dim,
                           condkwidth=opts.condkwidth,
                           z_hid_sum=opts.z_hid_sum)
        self.G.apply(wsegan_weights_init)
        self.dec_type = opts.gdec_type
        if self.dec_type == 'conddeconv':
            # Build embedding layer from spks to
            # cond_dim
            self.cond_emb = nn.Embedding(opts.cond_classes, opts.cond_dim)

    def build_discriminator(self, opts):
        dkwidth = opts.gkwidth if opts.dkwidth is None else opts.dkwidth
        projs = []
        if opts.utt2class is not None and len(opts.utt2class) >= 1:
            for utt2class in opts.utt2class:
                with open(utt2class, 'r') as uf:
                    u2c = json.load(uf)
                    dc = True if isinstance(list(u2c.values())[0], int) else False
                    if dc:
                        nc = max(list(u2c.values())) + 1
                    else:
                        nc = len(list(u2c.values())[0])
                    projs.append(DProjector(256, nc, dc))
        self.D = AcoDiscriminator(2, opts.douts,
                                  opts.denc_fmaps, dkwidth,
                                  poolings=opts.denc_poolings,
                                  pool_slen=opts.dpool_slen, 
                                  aco_level=opts.daco_level,
                                  norm_type=opts.dnorm_type,
                                  bias=opts.bias,
                                  phase_shift=opts.phase_shift,
                                  projectors=projs)
        self.D.apply(wsegan_weights_init)

    def sample_dloader(self, iterator, dloader, device='cpu'):
        try:
            sample = next(iterator)
        except StopIteration:
            iterator = iter(dloader)
            sample = next(iterator)
        batch = sample
        proj_labs = []
        slice_idx = 0
        lab = torch.zeros(1)
        if len(batch) == 2:
            clean, noisy = batch
        elif len(batch) == 3:
            if self.disable_aco:
                clean, noisy, proj_labs = batch
                proj_labs = proj_labs.to(device)
                proj_labs = [proj_labs]
            else:
                lab, clean, noisy = batch
        elif len(batch) > 3:
            proj_labs = batch[3:]
            for i in range(len(proj_labs)):
                proj_labs[i] = proj_labs[i].to(device)
            batch = batch[:3]
            lab, clean, noisy = batch
        else:
            raise ValueError('Not enough dataset elements')
        clean = clean.unsqueeze(1)
        noisy = noisy.unsqueeze(1)
        clean = clean.to(device)
        noisy = noisy.to(device)
        lab = lab.to(device)
        return lab, clean, noisy, proj_labs

    def infer_G(self, nwav, cwav=None, z=None, ret_hid=False):
        Genh = self.G(nwav, z=z, ret_hid=ret_hid)
        return Genh

    def generate(self, inwav, cond=None, z=None):
        self.G.eval()
        if hasattr(self, 'cond_emb') and cond is not None:
            print('Forwarding cond through embedding')
            cond = self.cond_emb(cond)
        ori_len = inwav.size(2)
        p_wav = make_divN(inwav.transpose(1, 2), 1024).transpose(1, 2)
        c_res, hall = self.G(p_wav, z=z, cond=cond, ret_hid=True)
        c_res = c_res[0, 0, :ori_len].cpu().data.numpy()
        c_res = de_emphasize(c_res, self.preemph)
        return c_res, hall

    def train(self, opts, dloader, criterion, l1_init, l1_dec_step,
              l1_dec_epoch, log_freq, tr_samples=None, 
              va_dloader=None, frontend=None, device='cpu'):

        """ Train the GSEGAN """
        CUDA = device == 'cuda'
        self.num_devices = opts.num_devices

        # create writer to log out losses and stuff
        self.writer = SummaryWriter(os.path.join(opts.save_path, 'train'))

        # Build the optimizers
        Gopt, Dopt = self.build_optimizers(opts)

        # attach opts to models so that they are saved altogether in ckpts
        self.G.optim = Gopt
        self.D.optim = Dopt
        
        # Build savers for end of epoch, storing up to 3 epochs each
        eoe_g_saver = Saver(self.G, opts.save_path, max_ckpts=3,
                            optimizer=self.G.optim, prefix='EOE_G-')
        eoe_d_saver = Saver(self.D, opts.save_path, max_ckpts=3,
                            optimizer=self.D.optim, prefix='EOE_D-')
        # compute batches per epoch to iterate correctly through enough chunks
        # depending on slice_size
        bpe = (tr_samples // opts.slice_size) // opts.batch_size if tr_samples is not None else len(dloader)
        num_batches = len(dloader) 
        l1_weight = l1_init
        iteration = 1
        timings = []
        evals = {}
        noisy_evals = {}
        noisy_samples = None
        clean_samples = None
        z_sample = None
        lab = None
        cond = None
        patience = opts.patience
        best_val_obj = np.inf
        
        # parallelize G and D?
        if self.num_devices > 1 and CUDA:
            G = nn.DataParallel(self.G)
            D = nn.DataParallel(self.D)
        else:
            G = self.G
            D = self.D
        G.to(device)
        if hasattr(self, 'cond_emb'):
            self.cond_emb.to(device)
        D.to(device)

        iterator = iter(dloader)

        for iteration in range(1, opts.epoch * bpe + 1):
            beg_t = timeit.default_timer()
            lab, clean, noisy, proj_labs = self.sample_dloader(iterator, 
                                                               dloader,
                                                               device)
            bsz = clean.size(0)
            D_in = torch.cat((clean, noisy), dim=1)
            # prepare aco branch activation if not disabled
            aco_branch = True if not self.disable_aco else False
            # FORWARD D real
            if aco_branch:
                aco_real, d_real, _ = D(D_in, labs=proj_labs, aco_branch=True)
            else:
                d_real, _ = D(D_in, labs=proj_labs)
            # real lab will contain the returned lab features from dataloader
            # plus the real flag branch, whereas fake will only contain fake
            # flag, and false fake will contain both again
            rl_lab = torch.ones(d_real.size()).to(device)
            fk_lab = -1 * torch.ones(d_real.size()).to(device)
            rl_aco_lab = lab
            # D real loss
            d_real_loss = criterion(d_real, rl_lab)
            if aco_branch:
                d_real_aco_loss = criterion(aco_real, rl_aco_lab)
            else:
                d_real_aco_loss = torch.zeros(1).to(device)

            if hasattr(self, 'cond_emb'):
                # Build G conditioning, assuming it is the first proj_lab
                # TODO: Please fix this weak assignent with proj_labs[0]
                cond_emb = self.cond_emb(proj_labs[0])
                cond = cond_emb
            # First batch will return all hidden feats with ret_hid flag
            if noisy_samples is None:
                Genh, Ghall = G(noisy, z=None, cond=cond, ret_hid=True)
            else:
                Genh = G(noisy, z=None, cond=cond)
            fake = Genh.detach()
            # FORWARD D fake
            D_in = torch.cat((fake, noisy), dim=1)
            d_fake, _ = D(D_in, labs=proj_labs)
            # D fake loss
            d_fake_loss = criterion(d_fake, fk_lab)

            d_weight = 0.5 # count only d_fake and d_real
            d_loss = d_real_loss + d_real_aco_loss

            if self.misalign_pair:
                clean_shuf = list(torch.chunk(clean, clean.size(0), dim=0))
                shuffle(clean_shuf)
                clean_shuf = torch.cat(clean_shuf, dim=0)
                #d_fake_shuf, _ = self.infer_D(clean, clean_shuf)
                D_shuf_in = torch.cat((clean, clean_shuf), dim=1)
                d_fake_shuf, _ = D(D_shuf_in, labs=proj_labs)
                d_fake_shuf_loss = criterion(d_fake_shuf, fk_lab)
                d_weight = 1 / 3 # count 3 components now
                d_fake_loss += d_fake_shuf_loss

            if self.interf_pair:
                # put interferring squared signals with random amplitude and
                # freq as fake signals mixed with clean data
                # TODO: Beware with hard-coded values! possibly improve this
                freqs = [250, 1000, 4000]
                amps = [0.01, 0.05, 0.1, 1]
                bsz = clean.size(0)
                squares = []
                t = np.linspace(0, 2, 32000)
                for _ in range(bsz):
                    f_ = random.choice(freqs)
                    a_ = random.choice(amps)
                    sq = a_ * signal.square(2 * np.pi * f_ * t)
                    sq = sq[:clean.size(-1)].reshape((1, -1))
                    squares.append(torch.FloatTensor(sq))
                squares = torch.cat(squares, dim=0).unsqueeze(1)
                if clean.is_cuda:
                    squares = squares.to('cuda')
                interf = clean + squares
                D_fake_inter_in = torch.cat((interf, noisy), dim=1)
                d_fake_inter, _ = D(D_fake_inter_in, labs=proj_labs)
                #d_fake_inter, _ = self.infer_D(interf, noisy)
                d_fake_inter_loss = criterion(d_fake_inter, fk_lab)
                d_weight = 1 / 4 # count 4 components in d loss now
                d_fake_loss += d_fake_inter_loss

            #d_loss = d_weight * d_loss
            d_loss += d_fake_loss
            Dopt.zero_grad()
            d_loss.backward()
            Dopt.step()

            D_fake__in = torch.cat((Genh, noisy), dim=1)
            if aco_branch:
                aco_fake_, d_fake_, _ = D(D_fake__in, labs=proj_labs,
                                          aco_branch=aco_branch)
            else:
                d_fake_, _ = D(D_fake__in, labs=proj_labs)
            #d_fake_, _ = self.infer_D(Genh, noisy)

            fk__lab = torch.zeros(d_fake_.size()).cuda()
            g_adv_loss = criterion(d_fake_,  fk__lab)
            if aco_branch:
                g_aco_loss = criterion(aco_fake_, rl_aco_lab)
            else:
                g_aco_loss = torch.zeros(1).to(device)

            # POWER Loss -----------------------------------
            if self.pow_weight > 0:
                # make stft of gtruth
                clean_stft = torch.stft(clean.squeeze(1), 
                                        n_fft=min(clean.size(-1), self.n_fft), 
                                        hop_length=160,
                                        win_length=320,
                                        normalized=True)
                clean_mod = torch.norm(clean_stft, 2, dim=3)
                clean_mod_pow = 10 * torch.log10(clean_mod ** 2 + 10e-20)
                Genh_stft = torch.stft(Genh.squeeze(1), 
                                       n_fft=min(Genh.size(-1), self.n_fft),
                                       hop_length=160, 
                                       win_length=320, normalized=True)
                Genh_mod = torch.norm(Genh_stft, 2, dim=3)
                Genh_mod_pow = 10 * torch.log10(Genh_mod ** 2 + 10e-20)
                pow_loss = self.pow_weight * F.l1_loss(Genh_mod_pow, clean_mod_pow)
            else:
                pow_loss = torch.zeros(1).to(device)
                clean_mod_pow = Genh_mod_pow = None

            G_cost = g_adv_loss + g_aco_loss + pow_loss
            Gopt.zero_grad()
            G_cost.backward()
            Gopt.step()
            end_t = timeit.default_timer()
            timings.append(end_t - beg_t)
            beg_t = timeit.default_timer()
            if noisy_samples is None:
                noisy_samples = noisy[:20, :, :].contiguous()
                clean_samples = clean[:20, :, :].contiguous()
            if z_sample is None and not self.G.no_z:
                # capture sample now that we know shape after first
                # inference
                z_sample = Ghall['z'][:20, :, :].contiguous()
                z_sample = z_sample.to(device)
            if iteration % log_freq == 0:
                log = 'Iter {}/{} ({} bpe) d_loss:{:.4f}, ' \
                      'g_loss: {:.4f} (g_adv_loss: {:.4f}, ' \
                      'g_aco_loss: {:.4f}, pow_loss: {:.4f}) ' \
                      ''.format(iteration,
                                bpe * opts.epoch,
                                bpe,
                                d_loss.item(),
                                G_cost.item(),
                                g_adv_loss.item(),
                                g_aco_loss.item(),
                                pow_loss.item())

                log += 'btime: {:.4f} s, mbtime: {:.4f} s' \
                       ''.format(timings[-1],
                                 np.mean(timings))
                print(log)
                self.writer.add_scalar('D_loss', d_loss.item(),
                                       iteration)
                self.writer.add_scalar('G_loss', G_cost.item(),
                                       iteration)
                self.writer.add_scalar('G_adv_loss', g_adv_loss.item(),
                                       iteration)
                self.writer.add_scalar('G_aco_loss', g_aco_loss.item(),
                                       iteration)
                self.writer.add_scalar('G_pow_loss', pow_loss.item(),
                                       iteration)
                if clean_mod_pow is not None:
                    self.writer.add_histogram('clean_mod_pow',
                                              clean_mod_pow.cpu().data,
                                              iteration,
                                              bins='sturges')
                    self.writer.add_histogram('Genh_mod_pow',
                                              Genh_mod_pow.cpu().data,
                                              iteration,
                                              bins='sturges')
                self.writer.add_histogram('Gz', Genh.cpu().data,
                                          iteration, bins='sturges')
                self.writer.add_histogram('clean', clean.cpu().data,
                                          iteration, bins='sturges')
                self.writer.add_histogram('noisy', noisy.cpu().data,
                                          iteration, bins='sturges')
                if hasattr(self.G, 'skips'):
                    for skip_id, skip in enumerate(self.G.skips):
                        if skip.skip_type == 'alpha':
                            self.writer.add_histogram('skip_alpha_{}'.format(skip_id),
                                                      skip.skip_k.data,
                                                      iteration, 
                                                      bins='sturges')
                        elif skip.skip_type == 'sconv':
                            self.writer.add_histogram('skip_sconv_{}'.format(skip_id),
                                                      skip.skip_k.conv.weight.data,
                                                      iteration, 
                                                      bins='sturges')
                # get D and G weights and plot their norms by layer and global
                def model_weights_norm(model, total_name):
                    total_GW_norm = 0
                    for k, v in model.named_parameters():
                        if 'weight' in k:
                            W = v.data
                            W_norm = torch.norm(W)
                            self.writer.add_scalar('{}_Wnorm'.format(k),
                                                   W_norm,
                                                   iteration)
                            total_GW_norm += W_norm
                    self.writer.add_scalar('{}_Wnorm'.format(total_name),
                                           total_GW_norm,
                                           iteration)
                model_weights_norm(self.G, 'Gtotal')
                model_weights_norm(self.D, 'Dtotal')
                if not opts.no_train_gen:
                    self.gen_train_samples(clean_samples, noisy_samples,
                                           z_sample,
                                           iteration=iteration)
                # BEWARE: There is no evaluation in Whisper SEGAN (WSEGAN)
                # TODO: Perhaps add some MCD/F0 RMSE metric
            if iteration % bpe == 0:
                # save models in end of epoch with EOE savers
                self.G.save(self.save_path, iteration, saver=eoe_g_saver)
                self.D.save(self.save_path, iteration, saver=eoe_d_saver)

class PASEGAN(WSEGAN):

    def __init__(self, opts, 
                 name='PASEGAN'):
        super().__init__(opts, name=name)

    def build_pase(self, opts):
        if not hasattr(self, 'pase'):
            self.pase = wf_builder(opts.pase_cfg)
            if opts.pase_ckpt is not None:
                self.pase.load_pretrained(opts.pase_ckpt, load_last=True,
                                          verbose=True)

    def build_generator(self, opts):
        pase = wf_builder(opts.pase_cfg)
        if opts.pase_ckpt is not None:
            pase.load_pretrained(opts.pase_ckpt, load_last=True,
                                      verbose=True)
        self.G = PASEGenerator(1, [128, 128, 128, 64],
                              [opts.gkwidth, opts.gkwidth, opts.gkwidth, opts.gkwidth],
                              [2, 4, 4, 5], z_dim=opts.z_dim,
                              frontend=pase,
                              norm_type=opts.gnorm_type,
                              ft_fe=opts.ft_fe)
        self.G.apply(pasegan_weights_init)

    def build_discriminator(self, opts):
        pase = wf_builder(opts.pase_cfg)
        if opts.pase_ckpt is not None:
            pase.load_pretrained(opts.pase_ckpt, load_last=True,
                                      verbose=True)
        self.D = RWDiscriminators(frontend=pase,
                                  ft_fe=opts.ft_fe,
                                  norm_type=opts.dnorm_type)
        self.D.apply(pasegan_weights_init)

    def sample_dloader(self, iterator, dloader, device='cpu'):
        try:
            sample = next(iterator)
        except StopIteration:
            iterator = iter(dloader)
            sample = next(iterator)
        batch = sample
        if len(batch) == 2:
            clean, noisy = batch
        else:
            raise ValueError('More than 2 elements from dataset?')
        clean = clean.unsqueeze(1)
        noisy = noisy.unsqueeze(1)
        clean = clean.to(device)
        noisy = noisy.to(device)
        return clean, noisy

    def infer_G(self, nwav, cwav=None, z=None, ret_hid=False):
        Genh = self.G(nwav, z=z, ret_hid=ret_hid)
        return Genh

    def generate(self, inwav, cond=None, z=None):
        self.G.eval()
        if hasattr(self, 'cond_emb') and cond is not None:
            print('Forwarding cond through embedding')
            cond = self.cond_emb(cond)
        ori_len = inwav.size(2)
        total_dec = np.cumprod(self.pase.strides)[-1]
        p_wav = make_divN(inwav.transpose(1, 2), total_dec).transpose(1, 2)
        c_res, hall = self.G(p_wav, z=z, cond=cond, ret_hid=True)
        c_res = c_res[0, 0, :ori_len].cpu().data.numpy()
        c_res = de_emphasize(c_res, self.preemph)
        return c_res, hall

    #@profile
    def train(self, opts, dloader, criterion, 
              log_freq, step_iters=1, tr_samples=None, 
              va_dloader=None, device='cpu'):

        """ Train the GSEGAN """
        CUDA = device == 'cuda'
        self.num_devices = opts.num_devices

        # create writer to log out losses and stuff
        self.writer = SummaryWriter(os.path.join(opts.save_path, 'train'))

        # Build the optimizers
        Gopt, Dopt = self.build_optimizers(opts)

        # attach opts to models so that they are saved altogether in ckpts
        self.G.optim = Gopt
        self.D.optim = Dopt
        
        # Build savers for end of epoch, storing up to 3 epochs each
        eoe_g_saver = Saver(self.G, opts.save_path, max_ckpts=3,
                            optimizer=self.G.optim, prefix='EOE_G-')
        eoe_d_saver = Saver(self.D, opts.save_path, max_ckpts=3,
                            optimizer=self.D.optim, prefix='EOE_D-')
        # compute batches per epoch to iterate correctly through enough chunks
        # depending on slice_size
        bpe = (tr_samples // opts.slice_size) // opts.batch_size if tr_samples is not None else len(dloader)
        num_batches = len(dloader) 
        timings = []
        evals = {}
        noisy_evals = {}
        noisy_samples = None
        clean_samples = None
        z_sample = None

        
        # parallelize G and D?
        if self.num_devices > 1 and CUDA:
            G = nn.DataParallel(self.G)
            D = nn.DataParallel(self.D)
        else:
            G = self.G
            D = self.D
        G.to(device)
        D.to(device)

        g_blacklist =[]
        if opts.g_ortho > 0.0:
            # create black-list if no ft_fe
            if not G.ft_fe:
                g_blacklist = [p for p in G.frontend.parameters()]

        d_blacklist =[]
        if opts.d_ortho > 0.0:
            # create black-list if no ft_fe
            if not D.ft_fe:
                d_blacklist = [p for p in D.frontend.parameters()]


        iterator = iter(dloader)

        for iteration in range(1, opts.epoch * bpe + 1):
            beg_t = timeit.default_timer()
            clean, noisy = self.sample_dloader(iterator, 
                                               dloader,
                                               device)
            bsz = clean.size(0)
            # Forward real data
            d_real = D(clean, cond=noisy)

            rl_lab = torch.ones(d_real.size()).to(device)
            fk_lab = -1 * torch.ones(d_real.size()).to(device)
            # D real loss
            if opts.hinge:
                d_real_loss = F.relu(1.0 - d_real).mean()
            else:
                d_real_loss = criterion(d_real, rl_lab)

            # First batch will return all hidden feats with ret_hid flag
            if noisy_samples is None:
                Genh, Ghall = G(noisy, z=None, ret_hid=True)
            else:
                Genh = G(noisy, z=None)

            fake = Genh.detach()
            # FORWARD D fake
            d_fake = D(fake, cond=noisy)
            # D fake loss
            if opts.hinge:
                d_fake_loss = F.relu(1.0 + d_fake).mean()
            else:
                d_fake_loss = criterion(d_fake, fk_lab)

            d_loss = (d_real_loss + d_fake_loss) / step_iters
            
            d_loss.backward()

            if opts.d_ortho > 0.0:
                # orthogonal regularization
                ortho_(D, opts.d_ortho, d_blacklist)

            if iteration % step_iters == 0:
                Dopt.step()
                Dopt.zero_grad()

            # re-gen G
            Genh = G(noisy, z=None)
            d_fake_ = D(Genh, cond=noisy)
            fk__lab = torch.zeros(d_fake_.size()).cuda()
            if opts.hinge:
                g_adv_loss = -d_fake_.mean()
            else:
                g_adv_loss = criterion(d_fake_,  fk__lab)

            # POWER Loss -----------------------------------
            if self.pow_weight > 0:
                # make stft of gtruth
                clean_stft = torch.stft(clean.squeeze(1), 
                                        n_fft=min(clean.size(-1), self.n_fft), 
                                        hop_length=160,
                                        win_length=320,
                                        normalized=True)
                clean_mod = torch.norm(clean_stft, 2, dim=3)
                clean_mod_pow = 10 * torch.log10(clean_mod ** 2 + 10e-20)
                Genh_stft = torch.stft(Genh.squeeze(1), 
                                       n_fft=min(Genh.size(-1), self.n_fft),
                                       hop_length=160, 
                                       win_length=320, normalized=True)
                Genh_mod = torch.norm(Genh_stft, 2, dim=3)
                Genh_mod_pow = 10 * torch.log10(Genh_mod ** 2 + 10e-20)
                pow_loss = self.pow_weight * F.l1_loss(Genh_mod_pow, clean_mod_pow)
            else:
                pow_loss = torch.zeros(1).to(device)
                clean_mod_pow = Genh_mod_pow = None

            G_cost = (g_adv_loss + pow_loss) / step_iters
            G_cost.backward()

            if opts.g_ortho > 0.0:
                # orthogonal regularization
                ortho_(G, opts.g_ortho, g_blacklist)

            if iteration % step_iters == 0:
                Gopt.step()
                Gopt.zero_grad()

            end_t = timeit.default_timer()
            timings.append(end_t - beg_t)
            beg_t = timeit.default_timer()
            if noisy_samples is None:
                noisy_samples = noisy[:20, :, :].contiguous()
                clean_samples = clean[:20, :, :].contiguous()
            if z_sample is None:
                # capture sample now that we know shape after first
                # inference
                z_sample = Ghall['z'][:20, :].contiguous()
                z_sample = z_sample.to(device)
            if iteration % log_freq == 0:
                #raise NotImplementedError
                log = 'Iter {}/{} ({} bpe) d_loss:{:.4f} (d_real: {:.4f}, ' \
                      'd_fake: {:.4f}), ' \
                      'g_loss: {:.4f} (g_adv_loss: {:.4f}, ' \
                      'pow_loss: {:.4f})' \
                      ''.format(iteration,
                                bpe * opts.epoch,
                                bpe,
                                d_loss.item(),
                                d_real_loss.item(),
                                d_fake_loss.item(),
                                G_cost.item(),
                                g_adv_loss.item(),
                                pow_loss.item())

                log += 'btime: {:.4f} s, mbtime: {:.4f} s' \
                       ''.format(timings[-1],
                                 np.mean(timings))
                print(log)
                self.writer.add_scalar('D_loss', d_loss.item(),
                                       iteration)
                self.writer.add_scalar('D_real_loss', d_real_loss.item(),
                                       iteration)
                self.writer.add_scalar('D_fake_loss', d_fake_loss.item(),
                                       iteration)
                self.writer.add_scalar('G_loss', G_cost.item(),
                                       iteration)
                self.writer.add_scalar('G_adv_loss', g_adv_loss.item(),
                                       iteration)
                self.writer.add_scalar('G_pow_loss', pow_loss.item(),
                                       iteration)
                if clean_mod_pow is not None:
                    self.writer.add_histogram('clean_mod_pow',
                                              clean_mod_pow.cpu().data,
                                              iteration,
                                              bins='sturges')
                    self.writer.add_histogram('Genh_mod_pow',
                                              Genh_mod_pow.cpu().data,
                                              iteration,
                                              bins='sturges')
                self.writer.add_histogram('Gz', Genh.cpu().data,
                                          iteration, bins='sturges')
                self.writer.add_histogram('clean', clean.cpu().data,
                                          iteration, bins='sturges')
                self.writer.add_histogram('noisy', noisy.cpu().data,
                                          iteration, bins='sturges')
                # get D and G weights and plot their norms by layer and global
                def model_weights_norm(model, total_name):
                    total_GW_norm = 0
                    for k, v in model.named_parameters():
                        if 'weight' in k:
                            W = v.data
                            W_norm = torch.norm(W)
                            self.writer.add_scalar('{}_Wnorm'.format(k),
                                                   W_norm,
                                                   iteration)
                            total_GW_norm += W_norm
                    self.writer.add_scalar('{}_Wnorm'.format(total_name),
                                           total_GW_norm,
                                           iteration)
                model_weights_norm(self.G, 'Gtotal')
                model_weights_norm(self.D, 'Dtotal')
                if not opts.no_train_gen:
                    self.gen_train_samples(clean_samples, noisy_samples,
                                           z_sample,
                                           iteration=iteration)
            if iteration % bpe == 0:
                # save models in end of epoch with EOE savers
                self.G.save(self.save_path, iteration, saver=eoe_g_saver)
                self.D.save(self.save_path, iteration, saver=eoe_d_saver)

class GSEGAN2(WSEGAN):

    def __init__(self, opts, 
                 name='GSEGAN2'):
        super().__init__(opts, name=name)
        raise NotImplementedError
        from pase.models.frontend import wf_builder
        gfe = wf_builder(opts.gfe_cfg)
        if opts.gfe_ckpt is not None:
            print('Loading pretrained G FE {}'.format(opts.gfe_ckpt))
            gfe.load_pretrained(opts.gfe_ckpt)
        dfe = wf_builder(opts.dfe_cfg)
        if opts.dfe_ckpt is not None:
            print('Loading pretrained D FE {}'.format(opts.dfe_ckpt))
            dfe.load_pretrained(opts.dfe_ckpt)
        # Build G and D
        self.G = GeneratorFE(1,
                             opts.gdec_fmaps,
                             opts.gdec_kwidth,
                             opts.gdec_poolings,
                             z_dim=opts.z_dim,
                             norm_type=opts.gnorm_type,
                             bias=opts.bias,
                             frontend=wf_builder(opts.gfe_cfg))
        #self.G.apply(wsegan_weights_init)
        dkwidth = opts.gkwidth if opts.dkwidth is None else opts.dkwidth
        self.D = DiscriminatorFE(#opts.denc_fmaps, opts.denc_poolings,
                                 #dkwidth, frontend=dfe,
                                 frontend=dfe,
                                 norm_type=opts.dnorm_type,
                                 phase_shift=opts.phase_shift)
        #self.D.apply(wsegan_weights_init)

    def sample_dloader(self, dloader, device='cpu'):
        sample = next(dloader.__iter__())
        batch = sample
        if len(batch) == 2:
            clean, noisy = batch
            lab = None
            slice_idx = 0
        elif len(batch) == 3:
            lab, clean, noisy = batch
            slice_idx = 0
        elif len(batch) == 4:
            lab, clean, noisy, slice_idx = batch
            slice_idx = slice_idx.to(device)
        else:
            raise ValueError('Not enough dataset elements')
        clean = clean.unsqueeze(1)
        noisy = noisy.unsqueeze(1)
        clean = clean.to(device)
        noisy = noisy.to(device)
        if lab is not None:
            lab = lab.to(device)
        return lab, clean, noisy, slice_idx

    def infer_G(self, nwav, cwav=None, z=None, ret_hid=False):
        Genh = self.G(nwav, z=z, ret_hid=ret_hid)
        return Genh

    def train(self, opts, dloader, criterion, l1_init, l1_dec_step,
              l1_dec_epoch, log_freq, tr_samples=None, 
              va_dloader=None, frontend=None, device='cpu'):

        """ Train the GSEGAN """
        CUDA = device == 'cuda'
        self.num_devices = opts.num_devices

        # create writer to log out losses and stuff
        self.writer = SummaryWriter(os.path.join(opts.save_path, 'train'))

        # Build the optimizers
        Gopt, Dopt = self.build_optimizers(opts)

        # attach opts to models so that they are saved altogether in ckpts
        self.G.optim = Gopt
        self.D.optim = Dopt
        
        # Build savers for end of epoch, storing up to 3 epochs each
        eoe_g_saver = Saver(self.G, opts.save_path, max_ckpts=3,
                            optimizer=self.G.optim, prefix='EOE_G-')
        eoe_d_saver = Saver(self.D, opts.save_path, max_ckpts=3,
                            optimizer=self.D.optim, prefix='EOE_D-')
        # compute batches per epoch to iterate correctly through enough chunks
        # depending on slice_size
        bpe = (tr_samples // opts.slice_size) // opts.batch_size if tr_samples is not None else len(dloader)
        num_batches = len(dloader) 
        l1_weight = l1_init
        iteration = 1
        timings = []
        evals = {}
        noisy_evals = {}
        noisy_samples = None
        clean_samples = None
        z_sample = None
        lab = None
        patience = opts.patience
        best_val_obj = np.inf
        
        # parallelize G and D?
        if self.num_devices > 1 and CUDA:
            G = nn.DataParallel(self.G)
            D = nn.DataParallel(self.D)
        else:
            G = self.G
            D = self.D
        G.to(device)
        D.to(device)

        for iteration in range(1, opts.epoch * bpe + 1):
            beg_t = timeit.default_timer()
            lab, clean, noisy, slice_idx = self.sample_dloader(dloader,
                                                               device)
            bsz = clean.size(0)
            # FORWARD D real
            d_real, _ = D(clean, noisy)
            rl_lab = torch.ones(d_real.size()).to(device)
            fk_lab = -1 * torch.ones(d_real.size()).to(device)
            # D real loss
            d_real_loss = criterion(d_real, rl_lab)

            # First batch will return all hidden feats with ret_hid flag
            if noisy_samples is None:
                Genh, Ghall = G(noisy, z=None, ret_hid=True)
            else:
                Genh = G(noisy, z=None)
            fake = Genh.detach()
            # FORWARD D fake
            d_fake, _ = D(fake, noisy)
            # D fake loss
            d_fake_loss = criterion(d_fake, fk_lab)

            d_weight = 0.5 # count only d_fake and d_real
            d_loss = d_real_loss

            if self.misalign_pair:
                clean_shuf = list(torch.chunk(clean, clean.size(0), dim=0))
                shuffle(clean_shuf)
                clean_shuf = torch.cat(clean_shuf, dim=0)
                #d_fake_shuf, _ = D(clean, clean_shuf)
                # TODO: try injecting clean_shuf and noisy instead
                d_fake_shuf, _ = D(clean_shuf, noisy)
                d_fake_shuf_loss = criterion(d_fake_shuf, fk_lab)
                d_weight = 1 / 3 # count 3 components now
                d_fake_loss += d_fake_shuf_loss

            if self.interf_pair:
                # put interferring squared signals with random amplitude and
                # freq as fake signals mixed with clean data
                # TODO: Beware with hard-coded values! possibly improve this
                freqs = [250, 1000, 4000]
                amps = [0.01, 0.05, 0.1, 1]
                bsz = clean.size(0)
                squares = []
                t = np.linspace(0, 2, 32000)
                for _ in range(bsz):
                    f_ = random.choice(freqs)
                    a_ = random.choice(amps)
                    sq = a_ * signal.square(2 * np.pi * f_ * t)
                    sq = sq[:clean.size(-1)].reshape((1, -1))
                    squares.append(torch.FloatTensor(sq))
                squares = torch.cat(squares, dim=0).unsqueeze(1)
                if clean.is_cuda:
                    squares = squares.to('cuda')
                interf = clean + squares
                d_fake_inter, _ = D(interf, noisy)
                #d_fake_inter, _ = self.infer_D(interf, noisy)
                d_fake_inter_loss = criterion(d_fake_inter, fk_lab)
                d_weight = 1 / 4 # count 4 components in d loss now
                d_fake_loss += d_fake_inter_loss

            #d_loss = d_weight * d_loss
            d_loss += d_fake_loss
            Dopt.zero_grad()
            d_loss.backward()
            Dopt.step()

            d_fake_, _ = D(Genh, noisy)

            fk__lab = torch.zeros(d_fake_.size()).cuda()
            g_adv_loss = criterion(d_fake_,  fk__lab)

            # POWER Loss -----------------------------------
            if self.pow_weight > 0:
                # make stft of gtruth
                clean_stft = torch.stft(clean.squeeze(1), 
                                        n_fft=min(clean.size(-1), self.n_fft), 
                                        hop_length=160,
                                        win_length=320,
                                        normalized=True)
                clean_mod = torch.norm(clean_stft, 2, dim=3)
                clean_mod_pow = 10 * torch.log10(clean_mod ** 2 + 10e-20)
                Genh_stft = torch.stft(Genh.squeeze(1), 
                                       n_fft=min(Genh.size(-1), self.n_fft),
                                       hop_length=160, 
                                       win_length=320, normalized=True)
                Genh_mod = torch.norm(Genh_stft, 2, dim=3)
                Genh_mod_pow = 10 * torch.log10(Genh_mod ** 2 + 10e-20)
                pow_loss = self.pow_weight * F.l1_loss(Genh_mod_pow, clean_mod_pow)
            else:
                pow_loss = torch.zeros(1).to(device)
                clean_mod_pow = Genh_mod_pow = None

            G_cost = g_adv_loss + pow_loss
            Gopt.zero_grad()
            G_cost.backward()
            Gopt.step()
            end_t = timeit.default_timer()
            timings.append(end_t - beg_t)
            beg_t = timeit.default_timer()
            if noisy_samples is None:
                noisy_samples = noisy[:20, :, :].contiguous()
                clean_samples = clean[:20, :, :].contiguous()
            if z_sample is None:
                # capture sample now that we know shape after first
                # inference
                z_sample = Ghall['z'][:20, :].contiguous()
                z_sample = z_sample.to(device)
            if iteration % log_freq == 0:
                log = 'Iter {}/{} ({} bpe) d_loss:{:.4f}, ' \
                      'g_loss: {:.4f} (g_adv_loss: {:.4f}, ' \
                      'pow_loss: {:.4f}) ' \
                      ''.format(iteration,
                                bpe * opts.epoch,
                                bpe,
                                d_loss.item(),
                                G_cost.item(),
                                g_adv_loss.item(),
                                pow_loss.item())

                log += 'btime: {:.4f} s, mbtime: {:.4f} s' \
                       ''.format(timings[-1],
                                 np.mean(timings))
                print(log)
                self.writer.add_scalar('D_loss', d_loss.item(),
                                       iteration)
                self.writer.add_scalar('G_loss', G_cost.item(),
                                       iteration)
                self.writer.add_scalar('G_adv_loss', g_adv_loss.item(),
                                       iteration)
                self.writer.add_scalar('G_pow_loss', pow_loss.item(),
                                       iteration)
                if clean_mod_pow is not None:
                    self.writer.add_histogram('clean_mod_pow',
                                              clean_mod_pow.cpu().data,
                                              iteration,
                                              bins='sturges')
                    self.writer.add_histogram('Genh_mod_pow',
                                              Genh_mod_pow.cpu().data,
                                              iteration,
                                              bins='sturges')
                self.writer.add_histogram('Gz', Genh.cpu().data,
                                          iteration, bins='sturges')
                self.writer.add_histogram('clean', clean.cpu().data,
                                          iteration, bins='sturges')
                self.writer.add_histogram('noisy', noisy.cpu().data,
                                          iteration, bins='sturges')
                # get D and G weights and plot their norms by layer and global
                def model_weights_norm(model, total_name):
                    total_GW_norm = 0
                    for k, v in model.named_parameters():
                        if 'weight' in k:
                            W = v.data
                            W_norm = torch.norm(W)
                            self.writer.add_scalar('{}_Wnorm'.format(k),
                                                   W_norm,
                                                   iteration)
                            total_GW_norm += W_norm
                    self.writer.add_scalar('{}_Wnorm'.format(total_name),
                                           total_GW_norm,
                                           iteration)
                model_weights_norm(self.G, 'Gtotal')
                model_weights_norm(self.D, 'Dtotal')
                if not opts.no_train_gen:
                    self.gen_train_samples(clean_samples, noisy_samples,
                                           z_sample,
                                           iteration=iteration)
                # BEWARE: There is no evaluation in Whisper SEGAN (WSEGAN)
                # TODO: Perhaps add some MCD/F0 RMSE metric
            if iteration % bpe == 0:
                # save models in end of epoch with EOE savers
                self.G.save(self.save_path, iteration, saver=eoe_g_saver)
                self.D.save(self.save_path, iteration, saver=eoe_d_saver)

class AEWSEGAN(WSEGAN):

    """ Auto-Encoder model """

    def __init__(self, opts, name='AEWSEGAN',
                 generator=None,
                 discriminator=None):
        super().__init__(opts, name=name, generator=generator,
                         discriminator=discriminator)
        # delete discriminator
        self.D = None

    def train(self, opts, dloader, criterion, l1_init, l1_dec_step,
              l1_dec_epoch, log_freq, va_dloader=None, device='cpu'):

        """ Train the SEGAN """
        # create writer
        self.writer = SummaryWriter(os.path.join(opts.save_path, 'train'))
        if opts.opt == 'rmsprop':
            Gopt = optim.RMSprop(self.G.parameters(), lr=opts.g_lr)
        elif opts.opt == 'adam':
            Gopt = optim.Adam(self.G.parameters(), lr=opts.g_lr, betas=(0.5,
                                                                        0.9))
        else:
            raise ValueError('Unrecognized optimizer {}'.format(opts.opt))

        # attach opts to models so that they are saved altogether in ckpts
        self.G.optim = Gopt
        
        # Build savers for end of epoch, storing up to 3 epochs each
        eoe_g_saver = Saver(self.G, opts.save_path, max_ckpts=3,
                            optimizer=self.G.optim, prefix='EOE_G-')
        num_batches = len(dloader) 
        l2_weight = l1_init
        iteration = 1
        timings = []
        evals = {}
        noisy_evals = {}
        noisy_samples = None
        clean_samples = None
        z_sample = None
        patience = opts.patience
        best_val_obj = np.inf
        # acumulator for exponential avg of valid curve
        acum_val_obj = 0
        G = self.G

        for iteration in range(1, opts.epoch * len(dloader) + 1):
            beg_t = timeit.default_timer()
            uttname, clean, noisy, slice_idx = self.sample_dloader(dloader,
                                                                   device)
            bsz = clean.size(0)
            Genh = self.infer_G(noisy, clean)
            Gopt.zero_grad()
            if self.l1_loss:
                loss = F.l1_loss(Genh, clean)
            else:
                loss = F.mse_loss(Genh, clean)
            loss.backward()
            Gopt.step()
            end_t = timeit.default_timer()
            timings.append(end_t - beg_t)
            beg_t = timeit.default_timer()
            if noisy_samples is None:
                noisy_samples = noisy[:20, :, :].contiguous()
                clean_samples = clean[:20, :, :].contiguous()
            if z_sample is None and not G.no_z:
                # capture sample now that we know shape after first
                # inference
                z_sample = G.z[:20, :, :].contiguous()
                print('z_sample size: ', z_sample.size())
                z_sample = z_sample.to(device)
            if iteration % log_freq == 0:
                # POWER Loss (not used to backward) -----------------------------------
                # make stft of gtruth
                clean_stft = torch.stft(clean.squeeze(1), 
                                        n_fft=min(clean.size(-1), self.n_fft), 
                                        hop_length=160,
                                        win_length=320,
                                        normalized=True)
                clean_mod = torch.norm(clean_stft, 2, dim=3)
                clean_mod_pow = 10 * torch.log10(clean_mod ** 2 + 10e-20)
                Genh_stft = torch.stft(Genh.detach().squeeze(1), 
                                       n_fft=min(Genh.size(-1), self.n_fft),
                                       hop_length=160, 
                                       win_length=320, normalized=True)
                Genh_mod = torch.norm(Genh_stft, 2, dim=3)
                Genh_mod_pow = 10 * torch.log10(Genh_mod ** 2 + 10e-20)
                pow_loss = F.l1_loss(Genh_mod_pow, clean_mod_pow)
                log = 'Iter {}/{} ({} bpe) g_l2_loss:{:.4f}, ' \
                      'pow_loss: {:.4f}, ' \
                      ''.format(iteration,
                                len(dloader) * opts.epoch,
                                len(dloader),
                                loss.item(),
                                pow_loss.item())

                log += 'btime: {:.4f} s, mbtime: {:.4f} s' \
                       ''.format(timings[-1],
                                 np.mean(timings))
                print(log)
                self.writer.add_scalar('g_l2/l1_loss', loss.item(),
                                       iteration)
                self.writer.add_scalar('G_pow_loss', pow_loss.item(),
                                       iteration)
                self.writer.add_histogram('clean_mod_pow',
                                          clean_mod_pow.cpu().data,
                                          iteration,
                                          bins='sturges')
                self.writer.add_histogram('Genh_mod_pow',
                                          Genh_mod_pow.cpu().data,
                                          iteration,
                                          bins='sturges')
                self.writer.add_histogram('Gz', Genh.cpu().data,
                                          iteration, bins='sturges')
                self.writer.add_histogram('clean', clean.cpu().data,
                                          iteration, bins='sturges')
                self.writer.add_histogram('noisy', noisy.cpu().data,
                                          iteration, bins='sturges')
                if hasattr(G, 'skips'):
                    for skip_id, alpha in G.skips.items():
                        skip = alpha['alpha']
                        if skip.skip_type == 'alpha':
                            self.writer.add_histogram('skip_alpha_{}'.format(skip_id),
                                                      skip.skip_k.data,
                                                      iteration, 
                                                      bins='sturges')
                # get D and G weights and plot their norms by layer and global
                def model_weights_norm(model, total_name):
                    total_GW_norm = 0
                    for k, v in model.named_parameters():
                        if 'weight' in k:
                            W = v.data
                            W_norm = torch.norm(W)
                            self.writer.add_scalar('{}_Wnorm'.format(k),
                                                   W_norm,
                                                   iteration)
                            total_GW_norm += W_norm
                    self.writer.add_scalar('{}_Wnorm'.format(total_name),
                                           total_GW_norm,
                                           iteration)
                #model_weights_norm(G, 'Gtotal')
                #model_weights_norm(D, 'Dtotal')
                if not opts.no_train_gen:
                    #canvas_w = self.G(noisy_samples, z=z_sample)
                    self.gen_train_samples(clean_samples, noisy_samples,
                                           z_sample,
                                           iteration=iteration)
                if va_dloader is not None:
                    if len(noisy_evals) == 0:
                        sd, nsd = self.evaluate(opts, va_dloader,
                                                log_freq, do_noisy=True)
                        self.writer.add_scalar('noisy_SD',
                                               nsd, iteration)
                    else:
                        sd = self.evaluate(opts, va_dloader, 
                                           log_freq, do_noisy=False)
                    self.writer.add_scalar('Genh_SD',
                                           sd, iteration)
                    print('Eval SD: {:.3f} dB, NSD: {:.3f} dB'.format(sd, nsd))
                    if sd < best_val_obj:
                        self.G.save(self.save_path, iteration, True)
                        best_val_obj = sd
            if iteration % len(dloader) == 0:
                # save models in end of epoch with EOE savers
                self.G.save(self.save_path, iteration, saver=eoe_g_saver)
