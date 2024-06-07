"""CLI"""

import logging
import re
import select
import sys

import click

from .bip32 import to_master_key
from .bip32types import parse_ext_key
from .bip39 import (
    LANGUAGES,
    N_WORDS_ALLOWED,
    entropy_to_words,
    normalize_list,
    normalize_str,
    to_master_seed,
    validate_mnemonic_words,
)
from .bip85 import (
    APPLICATIONS,
    DRNG,
    INDEX_TO_LANGUAGE,
    PURPOSE_CODES,
    RANGES,
    apply_85,
    derive,
    to_entropy,
)
from .util import (
    LOGGER,
    MIN_REL_ENTROPY,
    __app_name__,
    __version__,
    relative_entropy,
    to_hex_string,
)

ISO_TO_LANGUAGE = {v["code"]: k for k, v in LANGUAGES.items()}

MNEMONIC_TO_VALUES = list(ISO_TO_LANGUAGE.keys())

SEED_FROM_VALUES = [
    "any",
    "random",
] + list(ISO_TO_LANGUAGE.keys())


SEED_TO_VALUES = [
    "tprv",
    "xprv",
] + list(ISO_TO_LANGUAGE.keys())


ENTROPY_TO_VALUES = list(ISO_TO_LANGUAGE.keys())

N_WORDS_ALLOWED_STR = [str(n) for n in N_WORDS_ALLOWED]

TIMEOUT = 0.08


logger = logging.getLogger(LOGGER)


@click.group()
@click.version_option(version=__version__, prog_name=__app_name__)
def cli():
    pass


@click.command(
    name="mnemonic", help="Generate a BIP-39 mnemonic from `secrets.randbits`."
)
@click.option(
    "-t",
    "--to",
    "to",
    type=click.Choice(MNEMONIC_TO_VALUES),
    help=("Mnemonic language 3-letter ISO code."),
    default="eng",
)
@click.option(
    "-n",
    "--number",
    default="24",
    type=click.Choice(N_WORDS_ALLOWED_STR),
    help="Number of mnemonic words.",
)
@click.option(
    "--pretty/--not-pretty",
    is_flag=True,
    default=False,
    help="Print a number before, and a newline after, each mnemonic word.",
)
def mnemonic(to, number, pretty):
    number = int(number)
    language = ISO_TO_LANGUAGE[to]
    mnemonic = entropy_to_words(number, None, language)
    if pretty:
        output = "\n".join(f"{i + 1}) {w}" for i, w in enumerate(mnemonic))
    else:
        output = " ".join(mnemonic)

    click.echo(output)


@click.command(
    name="validate",
    help="Validate and normalize a BIP-39 mnemonic into one string.",
)
@click.option(
    "-f",
    "--from",
    "from_",
    type=click.Choice(["free"] + MNEMONIC_TO_VALUES),
    help=("Mnemonic language 3-letter ISO code, or 'free' for any string."),
    default="eng",
)
@click.option(
    "-m",
    "--mnemonic",
    "mnemonic",
    help="String mnemonic in format given by --from.",
)
def validate(from_, mnemonic):
    if mnemonic:
        mnemonic = mnemonic.strip()
    else:
        mnemonic = look_for_pipe()
    # TODO: add to spec we still normalize and split on space always!
    words = normalize_list(re.split(r"\s+", mnemonic), lower=True)

    if from_ == "free":
        implied = relative_entropy(normalize_str(mnemonic, lower=True))
        if implied < MIN_REL_ENTROPY:
            click.secho(
                (
                    f"Warning: Relative entropy of input seems low ({implied:.2f})."
                    " Consider more complex --input."
                ),
                fg="yellow",
                err=True,
            )
    else:
        language = ISO_TO_LANGUAGE[from_]
        if not validate_mnemonic_words(words, language):
            raise click.BadParameter(
                f"One or more non-{ISO_TO_LANGUAGE[from_]} words, or bad checksum,"
                f" or invalid number of words {len(words)}.",
                param_hint="--mnemonic",
            )

    click.echo(" ".join(words))


@click.command(
    name="xprv",
    help="Derive a BIP-32 XPRV from a string (does not validate).",
)
@click.option("-m", "--mnemonic", help="Quoted mnemonic.")
@click.option("-p", "--passphrase", default="", help="BIP-39 passphrase.")
@click.option("--mainnet/--testnet", is_flag=True, default=True)
# TODO: pipe mnemonic
def xprv(mnemonic, passphrase, mainnet):
    if mnemonic:
        mnemonic = mnemonic.strip()
    else:
        mnemonic = look_for_pipe()

    mnemonic_list = re.split(r"\s+", mnemonic)
    seed = to_master_seed(mnemonic_list, passphrase)
    prv = to_master_key(seed, mainnet=mainnet, private=True)

    click.echo(prv)


@click.command(name="derive", help="Derives a secret according to BIP-85.")
@click.option(
    "-a",
    "--application",
    required=True,
    type=click.Choice(APPLICATIONS.keys()),
)
@click.option(
    "-n",
    "--number",
    type=click.IntRange(min=1),
    help="Length of output in bytes, chars, or words, depending on --application.",
)
@click.option(
    "-i",
    "--index",
    type=click.IntRange(0, 2**31 - 1),
    default=0,
    help="Child index. Increment for fresh secrets.",
)
@click.option(
    "-s",
    "--special",
    default=10,
    type=click.IntRange(min=2),
    help="Number of sides for `--application dice`.",
)
@click.option(
    "-x",
    "--xprv",
    help="Extended private master key from which all secrets are derived.",
)
@click.option(
    "-t",
    "--to",
    type=click.Choice(ENTROPY_TO_VALUES),
    help="Output language for `--application mnemonic`.",
)
def derive_cli(application, number, index, special, xprv, to):
    if not xprv:
        xprv = look_for_pipe()
    else:
        xprv = xprv.strip()

    if xprv[:4] not in ("xprv", "tprv") or len(xprv) != 111:
        raise click.BadParameter(f"{xprv}", param_hint="--xprv (or pipe)")

    if number is not None:
        if application in ("wif", "xprv"):
            raise click.BadOptionUsage(
                option_name="--number",
                message="`--number` has no effect when `--application wif|xprv`",
            )
    else:
        number = 24

    master = parse_ext_key(xprv)

    path = f"m/{PURPOSE_CODES['BIP-85']}"
    app_code = APPLICATIONS[application]
    path += f"/{app_code}"

    if to:
        if application != "mnemonic":
            raise click.BadOptionUsage(
                option_name="--to",
                message="--to requires `--application mnemonic`",
            )
    else:
        to = "eng"

    if application == "mnemonic":
        language = ISO_TO_LANGUAGE[to]
        code_85 = next(i for i, l in INDEX_TO_LANGUAGE.items() if l == language)
        path += f"/{code_85}/{number}'/{index}'"
    elif application in ("wif", "xprv"):
        path += f"/{index}'"
    elif application in ("base64", "base85", "hex"):
        check_range(number, application)
        path += f"/{number}'/{index}'"
    elif application == "drng":
        path += f"/0'/{index}'"
    elif application == "dice":
        check_range(number, application)
        path += f"/{special}'/{number}'/{index}'"
    else:
        raise click.BadOptionUsage(
            option_name="--application",
            message=f"unrecognized {application}",
        )

    derived = derive(master, path)
    if application == "drng":
        drng = DRNG(to_entropy(derived.data[1:]))
        output = to_hex_string(drng.read(number))
    else:
        output = apply_85(derived, path)["application"]
    click.echo(output)


cli.add_command(mnemonic)
cli.add_command(validate)
cli.add_command(xprv)
cli.add_command(derive_cli)


def check_range(number: int, application: str):
    (min, max) = RANGES[application]
    if not (min <= number <= max):
        raise click.BadOptionUsage(
            option_name="--number",
            message=f"--number out of range. Try [{min}, {max}] for {application}.",
        )


def look_for_pipe():
    stdin, _, _ = select.select([sys.stdin], [], [], TIMEOUT)
    if stdin:
        lines = sys.stdin.readlines()
        if lines:
            # get just the last line because there might be a warning above
            return lines[-1].strip()


if __name__ == "__main__":
    cli()
