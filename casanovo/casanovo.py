"""The command line entry point for Casanovo."""
import collections
import csv
import datetime
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

import click
import psutil
import pytorch_lightning as pl
import torch
import yaml

from . import __version__
from casanovo.denovo import model_runner


logger = logging.getLogger("casanovo")


@click.command()
@click.option(
    "--mode",
    required=True,
    default="denovo",
    help="\b\nThe mode in which to run Casanovo:\n"
    '- "denovo" will predict peptide sequences for\nunknown MS/MS spectra.\n'
    '- "train" will train a model (from scratch or by\ncontinuing training a '
    "previously trained model).\n"
    '- "eval" will evaluate the performance of a\ntrained model using '
    "previously acquired spectrum\nannotations.",
    type=click.Choice(["denovo", "train", "eval"]),
)
@click.option(
    "--model",
    help="The file name of the model weights (.ckpt file).",
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--peak_path",
    required=True,
    help="The file path with peak files for predicting peptide sequences or "
    "training Casanovo.",
)
@click.option(
    "--peak_path_val",
    help="The file path with peak files to be used as validation data during "
    "training.",
)
@click.option(
    "--config",
    help="The file name of the configuration file with custom options. If not "
    "specified, a default configuration will be used.",
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--output",
    help="The base output file name to store logging (extension: .log) and "
    "(optionally) prediction results (extension: .csv).",
    type=click.Path(dir_okay=False),
)
def main(
    mode: str,
    model: str,
    peak_path: str,
    peak_path_val: str,
    config: str,
    output: str,
):
    """
    \b
    Casanovo: De novo mass spectrometry peptide sequencing with a transformer model.
    ================================================================================

    Yilmaz, M., Fondrie, W. E., Bittremieux, W., Oh, S. & Noble, W. S. De novo
    mass spectrometry peptide sequencing with a transformer model. Proceedings
    of the 39th International Conference on Machine Learning - ICML '22 (2022)
    doi:10.1101/2022.02.07.479481.

    Official code website: https://github.com/Noble-Lab/casanovo
    """
    if output is None:
        output = os.path.join(
            os.getcwd(),
            f"casanovo_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}",
        )
    else:
        output = os.path.splitext(os.path.abspath(output))[0]

    # Configure logging.
    logging.captureWarnings(True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    log_formatter = logging.Formatter(
        "{asctime} {levelname} [{name}/{processName}] {module}.{funcName} : "
        "{message}",
        style="{",
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(log_formatter)
    root.addHandler(console_handler)
    file_handler = logging.FileHandler(f"{output}.log")
    file_handler.setFormatter(log_formatter)
    root.addHandler(file_handler)
    # Disable dependency non-critical log messages.
    logging.getLogger("depthcharge").setLevel(logging.INFO)
    logging.getLogger("h5py").setLevel(logging.WARNING)
    logging.getLogger("numba").setLevel(logging.WARNING)
    logging.getLogger("pytorch_lightning").setLevel(logging.WARNING)
    logging.getLogger("torch").setLevel(logging.WARNING)

    # Read parameters from the config file.
    if config is None:
        config = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "config.yaml"
        )
    config_fn = config
    with open(config) as f_in:
        config = yaml.safe_load(f_in)
    # Ensure that the config values have the correct type.
    config_types = dict(
        random_seed=int,
        n_peaks=int,
        min_mz=float,
        max_mz=float,
        min_intensity=float,
        remove_precursor_tol=float,
        dim_model=int,
        n_head=int,
        dim_feedforward=int,
        n_layers=int,
        dropout=float,
        dim_intensity=int,
        max_length=int,
        max_charge=int,
        n_log=int,
        warmup_iters=int,
        max_iters=int,
        learning_rate=float,
        weight_decay=float,
        train_batch_size=int,
        predict_batch_size=int,
        max_epochs=int,
        num_sanity_val_steps=int,
        strategy=str,
        train_from_scratch=bool,
        save_model=bool,
        model_save_folder_path=str,
        save_weights_only=bool,
        every_n_epochs=int,
    )
    for k, t in config_types.items():
        try:
            if config[k] is not None:
                config[k] = t(config[k])
        except (TypeError, ValueError) as e:
            logger.error("Incorrect type for configuration value %s: %s", k, e)
            raise TypeError(f"Incorrect type for configuration value {k}: {e}")
    config["residues"] = {
        str(aa): float(mass) for aa, mass in config["residues"].items()
    }
    # Add extra configuration options and scale by the number of GPUs.
    n_gpus = torch.cuda.device_count()
    config["n_workers"] = len(psutil.Process().cpu_affinity()) // n_gpus
    config["train_batch_size"] = config["train_batch_size"] // n_gpus

    pl.utilities.seed.seed_everything(seed=config["random_seed"], workers=True)

    # Log the active configuration.
    logger.info("Casanovo version %s", str(__version__))
    logger.debug("mode = %s", mode)
    logger.debug("model = %s", model)
    logger.debug("peak_path = %s", peak_path)
    logger.debug("peak_path_val = %s", peak_path_val)
    logger.debug("config = %s", config_fn)
    logger.debug("output = %s", output)
    for key, value in config.items():
        logger.debug("%s = %s", str(key), str(value))

    # Run Casanovo in the specified mode.
    if mode == "denovo":
        logger.info("Predict peptide sequences with Casanovo.")
        _write_mztab_header(
            f"{output}.mztab",
            peak_path,
            config,
            model=model,
            config_filename=config_fn,
        )
        try:
            model_runner.predict(peak_path, model, f"{output}.mztab", config)
        except:
            # Delete the mzTab file in case predicting failed somehow.
            os.remove(f"{output}.mztab")
    elif mode == "eval":
        logger.info("Evaluate a trained Casanovo model.")
        model_runner.evaluate(peak_path, model, config)
    elif mode == "train":
        logger.info("Train the Casanovo model.")
        model_runner.train(peak_path, peak_path_val, model, config)


def _write_mztab_header(
    filename_out: str, filename_in: str, config: Dict[str, Any], **kwargs
) -> None:
    """
    Write metadata information to an mzTab file header.

    Parameters
    ----------
    filename_out : str
        The name of the mzTab file.
    filename_in : str
        The name or directory of the input file(s).
    config : Dict[str, Any]
        The active configuration options.
    kwargs
        Additional configuration options (i.e. from command-line arguments).
    """
    # Derive the fixed and variable modifications from the residue alphabet.
    known_mods = {
        "+57.021": "[UNIMOD, UNIMOD:4, Carbamidomethyl, ]",
        "+15.995": "[UNIMOD, UNIMOD:35, Oxidation, ]",
        "+0.984": "[UNIMOD, UNIMOD:7, Deamidated, ]",
        "+42.011": "[UNIMOD, UNIMOD:1, Acetyl, ]",
        "+43.006": "[UNIMOD, UNIMOD:5, Carbamyl, ]",
        "-17.027": "[UNIMOD, UNIMOD:385, Ammonia-loss, ]",
    }
    residues = collections.defaultdict(set)
    for aa, mass in config["residues"].items():
        aa_mod = re.match(r"([A-Z]?)([+-]?(?:[0-9]*[.])?[0-9]+)", aa)
        if aa_mod is None:
            residues[aa].add(None)
        else:
            residues[aa_mod[1]].add(aa_mod[2])
    fixed_mods, variable_mods = [], []
    for aa, mods in residues.items():
        if len(mods) > 1:
            for mod in mods:
                if mod is not None:
                    variable_mods.append((aa, mod))
        elif None not in mods:
            fixed_mods.append((aa, mods.pop()))

    # Write the mzTab output file header.
    metadata = [
        ("mzTab-version", "1.0.0"),
        ("mzTab-mode", "Summary"),
        ("mzTab-type", "Identification"),
        (
            "description",
            f"Casanovo identification file "
            f"{os.path.splitext(os.path.basename(filename_out))[0]}",
        ),
        (
            "ms_run[1]-location",
            Path(os.path.abspath(filename_in)).as_uri(),
        ),
        (
            "psm_search_engine_score[1]",
            "[MS, MS:1001143, search engine specific score for PSMs, ]",
        ),
        ("software[1]", f"[MS, MS:1003281, Casanovo, {__version__}]"),
    ]
    if len(fixed_mods) == 0:
        metadata.append(
            (
                "fixed_mod[1]",
                "[MS, MS:1002453, No fixed modifications searched, ]",
            )
        )
    else:
        for i, (aa, mod) in enumerate(fixed_mods, 1):
            metadata.append(
                (
                    f"fixed_mod[{i}]",
                    known_mods.get(mod, f"[CHEMMOD, CHEMMOD:{mod}, , ]"),
                )
            )
            metadata.append((f"fixed_mod[{i}]-site", aa if aa else "N-term"))
    if len(variable_mods) == 0:
        metadata.append(
            (
                "variable_mod[1]",
                "[MS, MS:1002454, No variable modifications searched,]",
            )
        )
    else:
        for i, (aa, mod) in enumerate(variable_mods, 1):
            metadata.append(
                (
                    f"variable_mod[{i}]",
                    known_mods.get(mod, f"[CHEMMOD, CHEMMOD:{mod}, , ]"),
                )
            )
            metadata.append(
                (f"variable_mod[{i}]-site", aa if aa else "N-term")
            )
    for i, (key, value) in enumerate(kwargs.items(), 1):
        metadata.append((f"software[1]-setting[{i}]", f"{key} = {value}"))
    for i, (key, value) in enumerate(config.items(), len(kwargs) + 1):
        if key not in ("residues",):
            metadata.append((f"software[1]-setting[{i}]", f"{key} = {value}"))
    with open(filename_out, "w") as f_out:
        writer = csv.writer(f_out, delimiter="\t", lineterminator=os.linesep())
        for row in metadata:
            writer.writerow(["MTD", *row])


if __name__ == "__main__":
    main()
