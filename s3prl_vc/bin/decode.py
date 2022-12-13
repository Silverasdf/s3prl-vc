#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2019 Tomoki Hayashi
#  MIT License (https://opensource.org/licenses/MIT)

"""Decode with trained model."""

import argparse
import logging
import os
import time

import numpy as np
import soundfile as sf
import torch
import yaml

from tqdm import tqdm

from s3prl.nn import S3PRLUpstream, Featurizer

import s3prl_vc.models
from s3prl_vc.datasets.datasets import AudioSCPMelDataset
from s3prl_vc.utils import read_hdf5
from s3prl_vc.utils.data import pad_list
from s3prl_vc.utils.plot import plot_generated_and_ref_2d, plot_1d
from s3prl_vc.vocoder import Vocoder


def main():
    """Run decoding process."""
    parser = argparse.ArgumentParser(
        description=("Decode with trained model " "(See detail in bin/decode.py).")
    )
    parser.add_argument(
        "--scp",
        type=str,
        required=True,
        help=("kaldi-style wav.scp file. "),
    )
    parser.add_argument(
        "--trg-stats",
        type=str,
        required=True,
        help="stats file for target denormalization.",
    )
    parser.add_argument(
        "--outdir",
        type=str,
        required=True,
        help="directory to save generated speech.",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        required=True,
        help="checkpoint file to be loaded.",
    )
    parser.add_argument(
        "--config",
        default=None,
        type=str,
        help=(
            "yaml format configuration file. if not explicitly provided, "
            "it will be searched in the checkpoint directory. (default=None)"
        ),
    )
    parser.add_argument(
        "--verbose",
        type=int,
        default=1,
        help="logging level. higher is more logging. (default=1)",
    )
    args = parser.parse_args()

    # set logger
    if args.verbose > 1:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s",
        )
    elif args.verbose > 0:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.WARN,
            format="%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s",
        )
        logging.warning("Skip DEBUG/INFO messages")

    # check directory existence
    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir)

    # load config
    if args.config is None:
        dirname = os.path.dirname(args.checkpoint)
        args.config = os.path.join(dirname, "config.yml")
    with open(args.config) as f:
        config = yaml.load(f, Loader=yaml.Loader)
    config.update(vars(args))

    # setup device
    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")

    # load target stats for denormalization
    config["trg_stats"] = {
        "mean": torch.from_numpy(read_hdf5(args.trg_stats, "mean")).float().to(device),
        "scale": torch.from_numpy(read_hdf5(args.trg_stats, "scale"))
        .float()
        .to(device),
    }

    # get dataset
    dataset = AudioSCPMelDataset(
        args.scp,
        config,
        return_utt_id=True,
    )
    logging.info(f"The number of features to be decoded = {len(dataset)}.")
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=len(dataset))

    # define upstream model
    upstream_model = S3PRLUpstream(config["upstream"]).to(device)
    upstream_model.eval()
    upstream_featurizer = Featurizer(upstream_model).to(device)
    upstream_featurizer.eval()

    # get model and load parameters
    model_class = getattr(s3prl_vc.models, config["model_type"])
    model = model_class(
        upstream_featurizer.output_size,
        config["num_mels"],
        config["sampling_rate"]
        / config["hop_size"]
        * upstream_featurizer.downsample_rate
        / 16000,
        config["trg_stats"],
        **config["model_params"],
    ).to(device)
    model.load_state_dict(torch.load(args.checkpoint, map_location="cpu")["model"])
    model = model.eval().to(device)
    logging.info(f"Loaded model parameters from {args.checkpoint}.")

    # load vocoder
    if config.get("vocoder", False):
        vocoder = Vocoder(
            config["vocoder"]["checkpoint"],
            config["vocoder"]["config"],
            config["vocoder"]["stats"],
            config["trg_stats"],
            device,
        )

    # start generation
    with torch.no_grad():
        for utt_id, x, mel in dataset:
            xs = torch.from_numpy(x).unsqueeze(0).float().to(device)
            ilens = torch.LongTensor([x.shape[0]]).to(device)

            start_time = time.time()
            all_hs, all_hlens = upstream_model(xs, ilens)
            hs, hlens = upstream_featurizer(all_hs, all_hlens)
            outs, _ = model(hs, hlens, spk_embs=None)
            out = outs[0]
            logging.info(
                "inference speed = %.1f frames / sec."
                % (int(out.size(0)) / (time.time() - start_time))
            )

            plot_generated_and_ref_2d(
                out.cpu().numpy(),
                config["outdir"] + f"/outs/{utt_id}.png",
                ref=mel,
                origin="lower",
            )

            if not os.path.exists(os.path.join(config["outdir"], "wav")):
                os.makedirs(os.path.join(config["outdir"], "wav"), exist_ok=True)

            y, sr = vocoder.decode(out)
            sf.write(
                os.path.join(config["outdir"], "wav", f"{utt_id}.wav"),
                y.cpu().numpy(),
                sr,
                "PCM_16",
            )


if __name__ == "__main__":
    main()
