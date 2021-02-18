import os.path
import json
import requests
import numpy as np
import ftfy
from data.encoders import fetch_encoder, encode
import tensorflow as tf
import re
from functools import partial
import hashlib
from inputs import pred_input_batch
from utils import split_list

lambada_src_uri = 'https://storage.googleapis.com/gpt-2/data/lambada_test.jsonl'
normalization = 'NFKC'


# Note: this task is called "lambada" but it really refers to OpenAI's version
# of the task, which actually differs in some ways from the task described in
# the original paper. So, strictly speaking, accuracy values from this task
# should not be compared to accuracy values from the original lambada task.
# For more information, see
#   https://github.com/openai/gpt-2/issues/131

def lambada_create_tokens_data(params, path):
    with open(path, 'w') as f:
        req = requests.get(lambada_src_uri)
        req.raise_for_status()
        jsons = [json.loads(l) for l in req.iter_lines()]
        texts = [ftfy.fix_text(j['text'], normalization=normalization) for j in jsons]
        enc = fetch_encoder(params)
        arrays = [encode(enc, t) for t in texts]
        json.dump(arrays, f)
        return arrays


def lambada_read_or_create_tokens_data(params, path):
    # if you tell me where the file should go, i will helpfully create it for you
    if not os.path.exists(path):
        return lambada_create_tokens_data(params, path)
    with open(path) as f:
        return json.load(f)


def bin_pack(params, tokens_data):
    eos_token = params['eos_id']
    n_ctx = params['n_ctx']
    dummy_token = 1
    pad_batch_size = params['eval_batch_size']
    bins = []
    for a in tokens_data:
        if len(bins) == 0 or len(bins[-1]) + len(a) + 1 > n_ctx:
            bins.append([])
        bins[-1] += a
        bins[-1].append(eos_token)
    while len(bins) % pad_batch_size != 0:
        bins.append([])
    bins_array = np.full((len(bins), n_ctx), dummy_token, dtype=np.uint16)
    for i, b in enumerate(bins):
        bins_array[i, 0:len(b)] = b
    return bins_array


def lambada_init(params):
    ds_config = params['dataset_configs'][0]
    tokenizer_hash = hashlib.md5(ds_config['tokenizer_path'].encode()).hexdigest()
    lt_path = f"lambada_{tokenizer_hash}.json"
    assert lt_path.endswith('.json'), 'lambada_tokens_path must have extension json'
    tokens_data = lambada_read_or_create_tokens_data(params, lt_path)
    bins_array = bin_pack(params, tokens_data)
    params['lambada_tokens_path'] = lt_path
    params['lambada_n_steps'] = len(bins_array) // params['eval_batch_size']


def lambada_get_task_info(params):
    return {
        'n_steps': params['lambada_n_steps'],
    }


def wikitext_detokenizer(string):
    # contractions
    string = string.replace("s '", "s'")
    string = re.sub(r"/' [0-9]/", r"/'[0-9]/", string)
    # number separators
    string = string.replace(" @-@ ", "-")
    string = string.replace(" @,@ ", ",")
    string = string.replace(" @.@ ", ".")
    # punctuation
    string = string.replace(" : ", ": ")
    string = string.replace(" ; ", "; ")
    string = string.replace(" . ", ". ")
    string = string.replace(" ! ", "! ")
    string = string.replace(" ? ", "? ")
    string = string.replace(" , ", ", ")
    # double brackets
    string = re.sub(r"\(\s*([^\)]*?)\s*\)", r"(\1)", string)
    string = re.sub(r"\[\s*([^\]]*?)\s*\]", r"[\1]", string)
    string = re.sub(r"{\s*([^}]*?)\s*}", r"{\1}", string)
    string = re.sub(r"\"\s*([^\"]*?)\s*\"", r'"\1"', string)
    string = re.sub(r"'\s*([^']*?)\s*'", r"'\1'", string)
    # miscellaneous
    string = string.replace("= = = =", "====")
    string = string.replace("= = =", "===")
    string = string.replace("= =", "==")
    string = string.replace(" " + chr(176) + " ", chr(176))
    string = string.replace(" \n", "\n")
    string = string.replace("\n ", "\n")
    string = string.replace(" N ", " 1 ")
    string = string.replace(" 's", "'s")

    return string


# The LAMBADA evaluation code looks at the logits of each position just before an eos_token
def lambada_input(params):
    eos_token = 50256 if params['n_vocab'] >= 50257 else 0
    n_ctx = params['n_ctx']
    lt_path = params['lambada_tokens_path']
    tokens_data = lambada_read_or_create_tokens_data(params, lt_path)
    bins_array = bin_pack(params, tokens_data)
    dataset = tf.data.Dataset.from_tensor_slices(bins_array)

    def _get_output(bin):
        bin = tf.cast(bin, dtype=tf.int32)
        indexes = tf.range(n_ctx)
        results = tf.gather(bin, (indexes + 1) % n_ctx)
        eos_next_positions = tf.math.equal(tf.gather(bin, (indexes + 2) % n_ctx), eos_token)
        output = tf.where(eos_next_positions, results, tf.constant(eos_token, shape=[n_ctx]))
        bin = tf.reshape(bin, [n_ctx])
        bin = tf.cast(bin, dtype=tf.int32)
        output = tf.reshape(output, [n_ctx])
        output = tf.cast(output, dtype=tf.int32)
        return bin, output

    dataset = dataset.map(_get_output)
    dataset = dataset.batch(params['eval_batch_size'], drop_remainder=True)
    dataset = dataset.repeat()
    return dataset


def wikitext_create_tokens_data(params, path, version):
    assert version.lower() in ["wikitext2", "wikitext103"]
    wikitext2_src = "https://s3.amazonaws.com/research.metamind.io/wikitext/wikitext-2-raw-v1.zip"
    wikitext103_src = "https://s3.amazonaws.com/research.metamind.io/wikitext/wikitext-103-raw-v1.zip"
    version_src = wikitext103_src if version.lower() == "wikitext103" else wikitext2_src
    with open(path, 'w') as f:
        wikitext_path = f"./{version}-raw-v1.zip"
        os.system(f"wget {version_src} -O {wikitext_path}")
        os.makedirs(f"{version}", exist_ok=True)
        os.system(f"unzip {wikitext_path} -d {version}")
        n = 103 if version.lower() == "wikitext103" else 2
        with open(f"./{version}/wikitext-{n}-raw/wiki.test.raw", 'r') as wt:
            text = ftfy.fix_text(wikitext_detokenizer(wt.read()))
        enc = fetch_encoder(params)
        encoded_text = encode(enc, text)
        arrays = []
        for i in range(0, len(encoded_text), params["n_ctx"] - 1):
            arrays.append(encoded_text[i:i + params["n_ctx"] - 1])
        json.dump(arrays, f)
        return arrays


def wikitext_read_or_create_tokens_data(params, path, version):
    # if you tell me where the file should go, i will helpfully create it for you
    if not os.path.exists(path):
        return wikitext_create_tokens_data(params, path, version)
    with open(path) as f:
        return json.load(f)


def wikitext_init(params, version):
    ds_config = params['dataset_configs'][0]
    tokenizer_hash = hashlib.md5(ds_config['tokenizer_path'].encode()).hexdigest()
    wikitext_path = f"{version}_{tokenizer_hash}.json"
    tokens_data = wikitext_read_or_create_tokens_data(params, wikitext_path, version)
    bins_array = bin_pack(params, tokens_data)
    params['wikitext_path'] = wikitext_path
    params['wikitext_n_steps'] = len(bins_array) // params['eval_batch_size']


def wikitext_get_task_info(params, version):
    return {
        'n_steps': params['wikitext_n_steps'],
    }


def wikitext_input(params, version):
    eos_token = 50256 if params['n_vocab'] >= 50257 else 0
    n_ctx = params['n_ctx']
    wt_path = params['wikitext_path']
    tokens_data = wikitext_read_or_create_tokens_data(params, wt_path, version)
    bins_array = bin_pack(params, tokens_data)
    dataset = tf.data.Dataset.from_tensor_slices(bins_array)

    def _get_output(bin):
        bin = tf.cast(bin, dtype=tf.int32)
        indexes = tf.range(n_ctx)
        results = tf.gather(bin, (indexes + 1) % n_ctx)
        eos_next_positions = tf.math.equal(tf.gather(bin, (indexes + 2) % n_ctx), eos_token)
        output = tf.where(eos_next_positions, results, tf.constant(eos_token, shape=[n_ctx]))
        bin = tf.reshape(bin, [n_ctx])
        bin = tf.cast(bin, dtype=tf.int32)
        output = tf.reshape(output, [n_ctx])
        output = tf.cast(output, dtype=tf.int32)
        return bin, output

    dataset = dataset.map(_get_output)
    dataset = dataset.batch(params['eval_batch_size'], drop_remainder=True)
    dataset = dataset.repeat()
    return dataset


def coqa_create_tokens_data(params, path="coqa.json"):
    COQA_URL = "https://nlp.stanford.edu/data/coqa/coqa-dev-v1.0.json"
    raw_path = path.replace('.json', '_raw.json')
    os.system(f"wget {COQA_URL} -O {raw_path}")
    enc = fetch_encoder(params)
    with open(raw_path, 'rb') as f:
        data = json.load(f)
        _data = data['data']
        for item in _data:
            item_id = item['id']
            story = item['story']
            questions = [q['input_text'] for q in item['questions']]
            turn_ids = [q['turn_id'] for q in item['questions']]
            prompts = [story + "\nQ: " + q + "\nA: " for q in questions]
            prompt_ids = [f"{item_id}_{turn_id}" for turn_id in turn_ids]
            tokenized_prompts = [encode(enc, text) for text in prompts]
            item['prompts'] = dict(zip(prompt_ids, tokenized_prompts))
    with open(path, 'w') as f:
        json.dump(data, f)
    return data


def coqa_read_or_create_tokens_data(params, path):
    # if you tell me where the file should go, i will helpfully create it for you
    if not os.path.exists(path):
        return coqa_create_tokens_data(params, path)
    with open(path) as f:
        return json.load(f)


def coqa_init(params):
    ds_config = params['dataset_configs'][0]
    coqa_path = f"coqa_{hashlib.md5(ds_config['tokenizer_path'].encode()).hexdigest()}.json"
    coqa_read_or_create_tokens_data(params, coqa_path)
    params['coqa_path'] = coqa_path


def coqa_input(params):
    coqa_path = params['coqa_path']
    tokenized_json = coqa_read_or_create_tokens_data(params, coqa_path)
    prompts = [list(i['prompts'].values()) for i in tokenized_json['data']]
    keys = [list(i['prompts'].keys()) for i in tokenized_json['data']]
    prompts = split_list(prompts, params['predict_batch_size'])
    keys = split_list(keys, params['predict_batch_size'])

    encoder = fetch_encoder(params)

    def input_fn(p):
        return pred_input_batch(params, p, enc=encoder)

    return input_fn, prompts, keys


task_descriptors = {
    'lambada': {
        'init_fn': lambada_init,
        'get_task_info_fn': lambada_get_task_info,
        'input_fn': lambada_input,
    },
    'wikitext2': {
        'init_fn': partial(wikitext_init, version='wikitext2'),
        'get_task_info_fn': partial(wikitext_get_task_info, version='wikitext2'),
        'input_fn': partial(wikitext_input, version='wikitext2'),
    },
    'wikitext103': {
        'init_fn': partial(wikitext_init, version='wikitext103'),
        'get_task_info_fn': partial(wikitext_get_task_info, version='wikitext103'),
        'input_fn': partial(wikitext_input, version='wikitext103'),
    },
    'coqa': {
        'init_fn': coqa_init,
        'input_fn': coqa_input
    }
}
