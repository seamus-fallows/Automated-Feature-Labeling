# %%
import torch as t
from torch.nn import functional as F
from transformers import (
    GPT2Tokenizer,
    GPT2LMHeadModel,
    AdamW,
    AutoModelForCausalLM,
    AutoTokenizer,
)
from transformers.models.llama.modeling_llama import LlamaForCausalLM
import einops
import matplotlib.pyplot as plt
import numpy as np
from typing import Union, Optional, Tuple, Any
from torch import Tensor
from dataclasses import dataclass, field
from tqdm.notebook import tqdm
from jaxtyping import Int, Float
from typing import List, Dict
from collections import defaultdict
from torch.utils.data import DataLoader, Dataset
import datetime

llama_token = "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"


class CachedDataset(Dataset):
    def __init__(
        self,
        model,
        tokenizer,
        token_list, # TODO think about making this a tensor
        activation_list,
        magic_token_string: str = "magic",
        threshhold: float = 0.5,
        sentence_cache_device: Optional[str] = None,
        system_prompt: str = """ Your task is to assess if a given token (word) from some text represents a specified concept. Provide a rating based on this assessment:
                            If the token represents the concept, respond with "Rating: 1".
                            If the token does not represent the concept, respond with "Rating: 0".
                            Focus solely on the token and use the other text for context only. Be confident.
                            """,
    ):
        super().__init__()
        self.B_INST, self.E_INST = "[INST]", "[/INST]"
        self.B_SYS, self.E_SYS = "<<SYS>>\n", "\n<</SYS>>\n\n"

        device = model.device

        self.magic_token_ids = tokenizer.encode(magic_token_string)[1]
        self.tokenizer = tokenizer
        self.model = model
        self.system_prompt = system_prompt

        yes_label = tokenizer.encode("1")[-1]
        no_label = tokenizer.encode("0")[-1]

        systemprompt_ids = self.systemprompt_to_ids(tokenizer, self.system_prompt, delete_first_token=False)
        system_promt_cache = self.get_cache(systemprompt_ids.to(device))

        attention_masks = []
        max_len = max([len(tokens) for tokens in token_list])
        for tokens in token_list:
            tokens = list(tokens)
            attention_mask = [1] * (len(tokens) + systemprompt_ids.shape[1])
            attention_mask += [0] * (max_len - len(tokens))
            tokens += [tokenizer.eos_token_id] * (max_len - len(tokens))
            attention_masks.append(attention_mask)

        self.sentence_caches = []

        for sentence, attention_mask in tqdm(zip(token_list, attention_masks)):
            sentence_ids, attention_mask = self.sentence_to_ids(
                sentence, attention_mask
            )
            sentence_cache = self.get_cache(
                sentence_ids.to(device),
                prev_cache=system_promt_cache,
                attention_mask=attention_mask.to(device),
            )
            if sentence_cache_device is not None:
                sentence_cache = tuple(
                    tuple(kv.to(sentence_cache_device) for kv in cache_layer)
                    for cache_layer in sentence_cache
                )

            self.sentence_caches.append(sentence_cache)

        self.datapoint_counter = 0
        self.sentence_counter = 0

        self.datapoint_number_to_sentence_number = dict()

        self.label_list = []
        self.question_end_ids_list = []
        for sentence, activations in tqdm(zip(token_list, activation_list)):
            for token, activation in zip(sentence, activations):
                label = yes_label if activation > threshhold else no_label
                self.label_list.append(label)
                question_end_ids = self.question_end_to_ids(token)
                self.question_end_ids_list.append(question_end_ids)
                self.datapoint_number_to_sentence_number[
                    self.datapoint_counter
                ] = self.sentence_counter
                self.datapoint_counter += 1
            self.sentence_counter += 1

    def systemprompt_to_ids(self, tokenizer, systtem_prompt):
        prompt = self.B_INST + self.B_SYS + systtem_prompt + self.E_SYS + "Context: "
        ids = t.tensor(tokenizer.encode(prompt)).unsqueeze(0)
        return ids

    def get_cache(self, ids, prev_cache=None, attention_mask=None):
        with t.no_grad():
            if prev_cache is None:
                output = self.model(
                    ids, return_dict=True, attention_mask=attention_mask
                )
            else:
                output = self.model(
                    ids,
                    past_key_values=prev_cache,
                    return_dict=True,
                    attention_mask=attention_mask,
                )
        return output.past_key_values

    def sentence_to_ids(self, sentence, attention_mask):
        ids = t.tensor(sentence).unsqueeze(0)
        attention_mask = t.tensor(attention_mask).unsqueeze(0)
        return ids, attention_mask

    def question_end_to_ids(self, question_token_ids):
        text_0 = "Concept:"
        ids_0 = self.tokenizer.encode(text_0)[1:]
        text_1 = " Token:"
        ids_1 = self.tokenizer.encode(text_1)[1:]
        text_2 = self.E_INST + "The rating is "
        ids_2 = self.tokenizer.encode(text_2)[1:]
        ids = ids_0 + [self.magic_token_ids] + ids_1 + [question_token_ids] + ids_2
        return t.tensor(ids).unsqueeze(0)

    def __getitem__(self, idx):
        return (
            self.sentence_caches[self.datapoint_number_to_sentence_number[idx]],
            self.question_end_ids_list[idx],
            self.label_list[idx],
        )

    def __len__(self):
        return self.datapoint_counter


class CachedDataloader(DataLoader):
    def __init__(self, dataset, batch_size, shuffle, device):
        super().__init__(
            dataset, batch_size, shuffle, collate_fn=self.custom_collate_fn
        )
        self.device = device

    def custom_collate_fn(self, batch):
        # Unzip the batch
        caches, sentence_ids, labels = zip(*batch)

        t.cuda.empty_cache()

        batched_sentence_ids = t.cat(sentence_ids, dim=0).to(self.device)
        batched_labels = t.tensor(labels).to(self.device)

        cache_device = caches[0][0][0].device

        batched_caches = tuple([
            tuple(
                t.cat([cache_layer[i] for cache_layer in cache], dim=0)
                for i in range(len(cache[0]))
            )
            for cache in zip(*caches)
        ])

        if cache_device != self.device:
            batched_caches = tuple(
                tuple(kv.to(self.device) for kv in cache_layer)
                for cache_layer in batched_caches
            )

        return batched_caches, batched_sentence_ids, batched_labels

# %%
if __name__ == "__main__":
    llama_token = "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"
    device = t.device("cuda:0" if t.cuda.is_available() else "cpu")
    t.cuda.empty_cache()
    n_param = 7
    model = AutoModelForCausalLM.from_pretrained(
        f"meta-llama/Llama-2-{n_param}b-chat-hf", use_auth_token=llama_token
    ).to(device)
    tokenizer = AutoTokenizer.from_pretrained(
        "meta-llama/Llama-2-7b-chat-hf",
        ignore_mismatched_sizes=True,
        use_auth_token=llama_token,
    )
    tokenizer.pad_token = tokenizer.eos_token
# %%
if __name__ == "__main__":
    string_list = [
        "Lore ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
        "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.",
        "Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur.",
        "Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.",
        "Sed ut perspiciatis unde omnis iste natus error sit voluptatem accusantium doloremque laudantium, totam rem aperiam.",
    ] * 10
    token_list = [tokenizer.encode(string) for string in string_list]
    activation_list = [t.rand(len(tokens)) for tokens in token_list]

# %%
if __name__ == "__main__":
    example_dataset = CachedDataset(model, tokenizer, token_list, activation_list)
    dataloader = CachedDataloader(
        example_dataset, batch_size=50, shuffle=True, device=device
    )
# %%
if __name__ == "__main__":
    probs_on_label = np.array([])
    for sentence_cache, question_end_ids, label in tqdm(dataloader):
        output = model(
            question_end_ids, past_key_values=sentence_cache, return_dict=True
        )
        output_probs = F.softmax(output.logits, dim=-1)
        del output
        del sentence_cache
        prob_on_label = output_probs[0, -1, label].detach().cpu().numpy()
        probs_on_label = np.append(probs_on_label, prob_on_label)

    print(probs_on_label)
    print(np.mean(probs_on_label))


