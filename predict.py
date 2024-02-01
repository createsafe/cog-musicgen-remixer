# Prediction interface for Cog ⚙️
# https://github.com/replicate/cog/blob/main/docs/python.md

import os
import random

# We need to set `TRANSFORMERS_CACHE` before any imports, which is why this is up here.
MODEL_PATH = "/src/models/"
os.environ["TRANSFORMERS_CACHE"] = MODEL_PATH
os.environ["TORCH_HOME"] = MODEL_PATH

from typing import Optional
from cog import BasePredictor, Input, Path

# Model specific imports
from typing import List

import torchaudio
import typing as tp
import numpy as np

import torch

from audiocraft.models import MusicGen, MultiBandDiffusion
from audiocraft.solvers.compression import CompressionSolver
from audiocraft.models.loaders import (
    load_compression_model,
    load_lm_model,
)
from audiocraft.data.audio import audio_write

from audiocraft.models.builders import get_lm_model
from omegaconf import OmegaConf

import librosa

import subprocess
import math

from BeatNet.BeatNet import BeatNet
import allin1
import pytsmod as tsm

def _delete_param(cfg, full_name: str):
    parts = full_name.split('.')
    for part in parts[:-1]:
        if part in cfg:
            cfg = cfg[part]
        else:
            return
    OmegaConf.set_struct(cfg, False)
    if parts[-1] in cfg:
        del cfg[parts[-1]]
    OmegaConf.set_struct(cfg, True)

def load_ckpt(path, device, url=False):
    if url:
        loaded = torch.hub.load_state_dict_from_url(str(path))
    else:
        loaded = torch.load(str(path))
    cfg = OmegaConf.create(loaded['xp.cfg'])
    cfg.device = str(device)
    if cfg.device == 'cpu':
        cfg.dtype = 'float32'
    else:
        cfg.dtype = 'float16'
    _delete_param(cfg, 'conditioners.self_wav.chroma_chord.cache_path')
    _delete_param(cfg, 'conditioners.self_wav.chroma_stem.cache_path')
    _delete_param(cfg, 'conditioners.args.merge_text_conditions_p')
    _delete_param(cfg, 'conditioners.args.drop_desc_p')

    lm = get_lm_model(loaded['xp.cfg'])
    lm.load_state_dict(loaded['model']) 
    lm.eval()
    lm.cfg = cfg
    compression_model = CompressionSolver.model_from_checkpoint(cfg.compression_model_checkpoint, device=device)
    return MusicGen(f"{os.getenv('COG_USERNAME')}/musicgen-chord", compression_model, lm)

class Predictor(BasePredictor):
    def setup(self, weights: Optional[Path] = None):
        """Load the model into memory to make running multiple predictions efficient"""
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.mbd = MultiBandDiffusion.get_mbd_musicgen()

    def _load_model(
        self,
        model_path: str,
        cls: Optional[any] = None,
        load_args: Optional[dict] = {},
        model_id: Optional[str] = None,
        device: Optional[str] = None,
    ) -> MusicGen:

        if device is None:
            device = self.device

        compression_model = load_compression_model(
            model_id, device=device, cache_dir=model_path
        )
        lm = load_lm_model(model_id, device=device, cache_dir=model_path)
        
        return MusicGen(model_id, compression_model, lm)

    def predict(
        self,
        model_version: str = Input(
            description="Model type. Computations take longer when using `large` or `stereo` models.", default="stereo-chord",
            choices=["stereo-chord", "stereo-chord-large", "chord", "chord-large"]
        ),
        prompt: str = Input(
            description="A description of the music you want to generate.", default=None
        ),
        music_input: Path = Input(
            description="An audio file input for the remix.",
            default=None,
        ),
        multi_band_diffusion: bool = Input(
            description="If `True`, the EnCodec tokens will be decoded with MultiBand Diffusion. Not compatible with `stereo` models.",
            default=False,
        ),
        normalization_strategy: str = Input(
            description="Strategy for normalizing audio.",
            default="loudness",
            choices=["loudness", "clip", "peak", "rms"],
        ),
        # bpm_hard_sync: bool = Input(
        #     description="If `True`, respective downbeats aren't analyzed, but are calculated from the bpm value detected and the first downbeat recognized instead. If the input audio has a changing bpm value, must be set `False`.",
        #     default=True,
        # ),
        beat_sync_threshold: float = Input(
            description="When beat syncing, if the gap between generated downbeat timing and input audio downbeat timing is larger than `beat_sync_threshold`, consider the beats are not corresponding. If `None` or `-1`, `1.1/(bpm/60)` will be used as the value. 0.75 is a good value to set.",
            default=None,
        ),
        large_chord_voca: bool = Input(
            description="If `True`, more chords like 7th, diminished and etc are used. If `False` only 12 major and 12 minor chords are used.",
            default=True
        ),
        chroma_coefficient: float = Input(
            description="Coefficient value multiplied to multi-hot chord chroma.",
            default=1.0,
            ge=0.5,
            le=2.0
        ),
        top_k: int = Input(
            description="Reduces sampling to the k most likely tokens.", default=250
        ),
        top_p: float = Input(
            description="Reduces sampling to tokens with cumulative probability of p. When set to  `0` (default), top_k sampling is used.",
            default=0.0,
        ),
        temperature: float = Input(
            description="Controls the 'conservativeness' of the sampling process. Higher temperature means more diversity.",
            default=1.0,
        ),
        classifier_free_guidance: int = Input(
            description="Increases the influence of inputs on the output. Higher values produce lower-varience outputs that adhere more closely to inputs.",
            default=3,
        ),
        output_format: str = Input(
            description="Output format for generated audio.",
            default="wav",
            choices=["wav", "mp3"],
        ),
        return_instrumental: bool = Input(
            description="If `True`, the instrumental audio will also be returned.",
            default=False,
        ),
        seed: int = Input(
            description="Seed for random number generator. If `None` or `-1`, a random seed will be used.",
            default=None,
        ),
        # overlap: int = Input(
        #     description="The length of overlapping part. Last `overlap` seconds of previous generation output audio is given to the next generation's audio prompt for continuation. (This will be fixed with the optimal value and be hidden, when releasing.)",
        #     default=5, le=15, ge=1
        # ),
        # in_step_beat_sync: bool = Input(
        #     description="If `True`, beat syncing is performed every generation step. In this case, audio prompting with EnCodec token will not be used, so that the audio quality might be degraded on and on along encoding-decoding sequences of the generation steps. (This will be fixed with the optimal value and be hidden, when releasing.)",
        #     default=False,
        # ),
        # amp_rate: float = Input(
        #     description="Amplifying the output audio to prevent volume diminishing along generations. (This will be fixed with the optimal value and be hidden, when releasing.)",
        #     default=1.2,
        # ),
    ) -> List[Path]:

        if prompt is None:
            raise ValueError("Must provide `prompt`.")
        if not music_input:
            raise ValueError("Must provide `music_input`.")
        
        if prompt is None:
            prompt = ''
        
        # tmp_path = 'tmp'
        # if os.path.isdir(tmp_path):
        #     import shutil
        #     shutil.rmtree(tmp_path)
        # os.mkdir(tmp_path)

        if os.path.isdir('demix'):
            import shutil
            shutil.rmtree('demix')
        if os.path.isdir('spec'):
            import shutil
            shutil.rmtree('spec')

        # Loading models
        if os.path.isfile(f'musicgen-{model_version}.th'):
                pass
        else:
            url = f"https://weights.replicate.delivery/default/musicgen-chord/musicgen-{model_version}.th"
            dest = f"/src/musicgen-{model_version}.th"
            subprocess.check_call(["pget", url, dest], close_fds=False)
        self.model = load_ckpt(f'/src/musicgen-{model_version}.th', self.device)
        self.model.lm.condition_provider.conditioners['self_wav'].match_len_on_eval = True

        if 'stereo' in model_version:
            channel = 2
        else:
            channel = 1

        if large_chord_voca is False:
            # Switching Chord Prediction model to 25 vocab (smaller)
            from audiocraft.modules.btc.btc_model import BTC_model
            from audiocraft.modules.btc.utils.mir_eval_modules import idx2chord
            self.model.lm.condition_provider.conditioners['self_wav'].chroma.config.feature['large_voca']=False
            self.model.lm.condition_provider.conditioners['self_wav'].chroma.config.model['num_chords']=25
            self.model.lm.condition_provider.conditioners['self_wav'].chroma.model_file='audiocraft/modules/btc/test/btc_model.pt'
            self.model.lm.condition_provider.conditioners['self_wav'].chroma.idx_to_chord = idx2chord
            loaded = torch.load('audiocraft/modules/btc/test/btc_model.pt')
            self.model.lm.condition_provider.conditioners['self_wav'].chroma.mean = loaded['mean']
            self.model.lm.condition_provider.conditioners['self_wav'].chroma.std = loaded['std']
            self.model.lm.condition_provider.conditioners['self_wav'].chroma.model = BTC_model(config=self.model.lm.condition_provider.conditioners['self_wav'].chroma.config.model).to(self.device)
            self.model.lm.condition_provider.conditioners['self_wav'].chroma.model.load_state_dict(loaded['model'])
            
        model = self.model
        model.lm.eval()

        if multi_band_diffusion and int(self.model.lm.cfg.transformer_lm.n_q) == 8:
            raise ValueError("Multi-band Diffusion only works with non-stereo models.")
        
        # in_step_beat_sync = in_step_beat_sync

        set_generation_params = lambda duration: model.set_generation_params(
            duration=duration,
            top_k=top_k,
            top_p=top_p,
            temperature=temperature,
            cfg_coef=classifier_free_guidance,
        )

        model.lm.condition_provider.conditioners['self_wav'].chroma_coefficient = chroma_coefficient

        if not seed or seed == -1:
            seed = torch.seed() % 2 ** 32 - 1
        set_all_seeds(seed)
        print(f"Using seed {seed}")

        # Music Structure Analysis
        music_input_analysis = allin1.analyze(music_input)

        music_input, sr = torchaudio.load(music_input)

        print("BPM : ", music_input_analysis.bpm)
        
        if not beat_sync_threshold or beat_sync_threshold == -1:
            if music_input_analysis.bpm is not None:
                beat_sync_threshold = 1.1/(int(music_input_analysis.bpm)/60)
            else:
                beat_sync_threshold = 0.75

        if music_input_analysis.bpm is not None:
            prompt = prompt + f', bpm : {int(music_input_analysis.bpm)}'
        
        music_input = music_input[None] if music_input.dim() == 2 else music_input
        duration = music_input.shape[-1]/sr
        wav_sr = model.sample_rate

        vocal, background = self.separate_vocals(music_input, sr)

        audio_write(
                "input_vocal",
                vocal[0].cpu(),
                model.sample_rate,
                strategy=normalization_strategy,
        )
        
        beat_sync_threshold = beat_sync_threshold
        # amp_rate = amp_rate

        set_generation_params(duration)

        with torch.no_grad():
            wav, tokens = model.generate_with_chroma([prompt], music_input, sr, progress=True, return_tokens=True)
            if multi_band_diffusion:
                wav = self.mbd.tokens_to_wav(tokens)

        mask_nan = torch.isnan(wav)
        mask_inf = torch.isinf(wav)

        wav[mask_nan] = 0  
        wav[mask_inf] = 1

        wav_amp = wav.abs().max()
        wav = (wav/wav_amp).cpu()
        # print(wav.abs().max())

        audio_write(
                "background",
                wav[0].cpu(),
                model.sample_rate,
                strategy=normalization_strategy,
        )
        
        wav_length = wav.shape[-1]


        """Start Here"""
        # if len(music_input_analysis.downbeats) > 0:

        #     estimator = BeatNet.BeatNet(1, mode='offline', inference_model='DBN', plot=[], thread=False)
        #     background_beat_estimation = estimator.process('background.wav')
        #     background_estimated_downbeats = [beat[0] for beat in background_beat_estimation if beat[1] == 1.0] 
            
        #     input_beat_estimation = estimator.process(music_input)
        #     input_estimated_downbeats = [beat[0] for beat in input_beat_estimation if beat[1] == 1.0] 

        #     wav_downbeats = []
        #     input_downbeats = []
        #     for wav_beat in background_estimated_downbeats:
        #         input_beat = min(input_estimated_downbeats, key=lambda x: abs(wav_beat - x), default=None)
        #         if input_beat is None:
        #             continue
        #         print(wav_beat, input_beat)
        #         if len(input_downbeats) != 0 and int(input_beat * wav_sr) == input_downbeats[-1]:
        #             print('Dropped')
        #             continue
        #         if abs(wav_beat-input_beat)>beat_sync_threshold:
        #             input_beat = wav_beat
        #             print('Replaced')
        #         wav_downbeats.append(int(wav_beat * wav_sr))
        #         input_downbeats.append(int(input_beat * wav_sr))

        #     downbeat_offset = input_downbeats[0]-wav_downbeats[0]
        #     # print(downbeat_offset)
        #     if downbeat_offset > 0:
        #         wav = torch.concat([torch.zeros([1,channel,int(downbeat_offset)]).cpu(),wav.cpu()],dim=-1)
        #         for i in range(len(wav_downbeats)):
        #             wav_downbeats[i]=wav_downbeats[i]+downbeat_offset
        #     wav_downbeats = [0] + wav_downbeats + [wav_length]
        #     input_downbeats = [0] + input_downbeats + [wav_length]

        #     # apply time stretching
        #     wav = torch.Tensor(tsm.wsola(wav[0].cpu().detach().numpy(), np.array([wav_downbeats, input_downbeats])))[...,:wav_length].unsqueeze(0).to(torch.float32)

        #     # Normalizing Audio
        #     mask_nan = torch.isnan(wav)
        #     mask_inf = torch.isinf(wav)
        #     wav[mask_nan] = 0
        #     wav[mask_inf] = 1

        #     wav_amp = wav.abs().max()
        #     if wav_amp != 0:
        #         wav = (wav/wav_amp).cpu()


        #     audio_write(
        #         "background_synced",
        #         wav[0].cpu(),
        #         model.sample_rate,
        #         strategy=normalization_strategy,
        #         loudness_compressor=True,
        #     )
        
        wav = wav.to(torch.float32)

        wav_amp = wav.abs().max()
        vocal_amp = vocal.abs().max()

        wav = 0.5*(wav/wav_amp).cpu()[...,:wav_length].cpu()*0.5

        output = torch.zeros(2, wav.size())
        output[0, :] = 0.5*(wav/wav_amp).cpu()[...,:wav_length].cpu()*0.5
        output[1, :] = 0.5*(vocal/vocal_amp).cpu()[...,:wav_length].cpu()*0.5
        
        # Normalizing Audio
        mask_nan = torch.isnan(wav)
        mask_inf = torch.isinf(wav)

        wav[mask_nan] = 0  
        wav[mask_inf] = 1

        wav_amp = wav.abs().max()
        if wav_amp != 0:
            wav = (wav/wav_amp).cpu()

        audio_write(
            "out",
            output.cpu(),
            model.sample_rate,
            strategy=normalization_strategy,
            loudness_compressor=True,
        )
        wav_path = "out.wav"

        if output_format == "mp3":
            mp3_path = "out.mp3"
            if Path(mp3_path).exists():
                os.remove(mp3_path)
            subprocess.call(["ffmpeg", "-i", wav_path, mp3_path])
            os.remove(wav_path)
            path = mp3_path
        else:
            path = wav_path

        output_dir = [Path(path)]

        if return_instrumental:
            inst_path = "background_synced.wav"

            if output_format == "mp3":
                mp3_inst_path = "background_synced.mp3"
                if Path(mp3_path).exists():
                    os.remove(mp3_path)
                subprocess.call(["ffmpeg", "-i", inst_path, mp3_inst_path])
                os.remove(inst_path)
                inst_out_path = mp3_inst_path
            else:
                inst_out_path = inst_path
            output_dir.append(Path(inst_out_path))

        return output_dir

    def _preprocess_audio(
        audio_path, model: MusicGen, duration: tp.Optional[int] = None
    ):

        wav, sr = torchaudio.load(audio_path)
        wav = torchaudio.functional.resample(wav, sr, model.sample_rate)
        wav = wav.mean(dim=0, keepdim=True)

        # Calculate duration in seconds if not provided
        if duration is None:
            duration = wav.shape[1] / model.sample_rate

        # Check if duration is more than 30 seconds
        if duration > 30:
            raise ValueError("Duration cannot be more than 30 seconds")

        end_sample = int(model.sample_rate * duration)
        wav = wav[:, :end_sample]

        assert wav.shape[0] == 1
        assert wav.shape[1] == model.sample_rate * duration

        wav = wav.cuda()
        wav = wav.unsqueeze(1)

        with torch.no_grad():
            gen_audio = model.compression_model.encode(wav)

        codes, scale = gen_audio

        assert scale is None

        return codes

    def estimate_beats(self, wav, sample_rate):
        # resample to BeatNet's sample rate
        beatnet_input = librosa.resample(
            wav,
            orig_sr=sample_rate,
            target_sr=self.beatnet.sample_rate,
        )
        return self.beatnet.process(beatnet_input)

    def separate_vocals(self, music_input, sr):
        from demucs.audio import convert_audio
        from demucs.apply import apply_model

        wav = convert_audio(music_input, sr, self.model.lm.condition_provider.conditioners['self_wav'].demucs.samplerate, self.model.lm.condition_provider.conditioners['self_wav'].demucs.audio_channels)
        stems = apply_model(self.model.lm.condition_provider.conditioners['self_wav'].demucs, wav, device=self.device)
        background = stems[:, self.model.lm.condition_provider.conditioners['self_wav'].demucs.sources.index('drums')] + stems[:, self.model.lm.condition_provider.conditioners['self_wav'].demucs.sources.index('other')] + stems[:, self.model.lm.condition_provider.conditioners['self_wav'].demucs.sources.index('bass')]
        vocals = stems[:, self.model.lm.condition_provider.conditioners['self_wav'].demucs.sources.index('vocals')]
        background = convert_audio(background, self.model.lm.condition_provider.conditioners['self_wav'].demucs.samplerate, self.model.sample_rate, 1)
        vocals = convert_audio(vocals, self.model.lm.condition_provider.conditioners['self_wav'].demucs.samplerate, self.model.sample_rate, 1)
        return vocals, background
    
# From https://gist.github.com/gatheluck/c57e2a40e3122028ceaecc3cb0d152ac
def set_all_seeds(seed):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
