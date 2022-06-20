from curses import raw
import logging, os, re, sys

logger = logging.getLogger(__name__)
import numpy as np
from pathlib import Path
import sys

import torch
from torch.utils.data import DataLoader

import datasets
import transformers
from transformers import (
    set_seed,
    Trainer,
    TrainingArguments,
    DebertaTokenizerFast,
    DebertaForTokenClassification,
)
from transformers.trainer_utils import get_last_checkpoint
from slue_toolkit.eval import eval_utils
from slue_toolkit.generic_utils import raw_to_combined_tag_map, load_pkl, read_lst


class VPDataset(torch.utils.data.Dataset):
    def __init__(self, encodings, labels):
        self.encodings = encodings
        self.labels = labels

    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item["labels"] = torch.tensor(self.labels[idx])
        return item

    def __len__(self):
        return len(self.labels)


class DataSetup:
    def __init__(self, data_dir, model_type):
        self.data_dir = data_dir
        self.tokenizer = DebertaTokenizerFast.from_pretrained(
            f"microsoft/{model_type}", add_prefix_space=True, output_loading_info=False
        )

    def read_data(self, file_path):
        file_path = Path(os.path.join(self.data_dir, file_path))

        raw_text = file_path.read_text().strip()
        raw_docs = re.split(r"\n\t?\n", raw_text)
        token_docs = []
        tag_docs = []
        for doc in raw_docs:
            tokens = []
            tags = []
            for line in doc.split("\n"):
                token, tag = line.split("\t")
                tokens.append(token)
                tags.append(tag)
            token_docs.append(tokens)
            tag_docs.append(tags)

        return token_docs, tag_docs

    def align_labels(self, tag2id, tags, encodings, label_all_tokens=False):
        """
        Align labels with appropriate padding labels for sub-tokens

        label_all_tokens: Whether to put the label for one word on all tokens of generated by that word or just on the
                         one (in which case the other tokens will have a padding index).
        """
        labels = [[tag2id[tag] for tag in doc] for doc in tags]
        encoded_labels = []
        for idx, doc_labels in enumerate(labels):
            word_ids = encodings.word_ids(batch_index=idx)
            previous_word_idx = None
            label_ids = []
            for word_idx in word_ids:
                # Special tokens have a word id that is None. We set the label to -100 so they are automatically
                # ignored in the loss function.
                if word_idx is None:
                    label_ids.append(-100)
                # We set the label for the first token of each word.
                elif word_idx != previous_word_idx:
                    label_ids.append(doc_labels[word_idx])
                # For the other tokens in a word, we set the label to either the current label or -100, depending on
                # the label_all_tokens flag.
                else:
                    label_ids.append(doc_labels[word_idx] if label_all_tokens else -100)
                previous_word_idx = word_idx

            encoded_labels.append(label_ids)
        return encoded_labels

    def prep_data(self, split_name, label_type="raw", eval_asr=False):
        if eval_asr:
            texts, tags = self.read_data(f"{split_name}.tsv")
        else:
            texts, tags = self.read_data(f"{split_name}_{label_type}.tsv")

        tag_id_fn = os.path.join(self.data_dir, f"{label_type}_tag2id.pkl")
        tag2id = load_pkl(tag_id_fn)

        # Tokenize data
        encodings = self.tokenizer(
            texts,
            is_split_into_words=True,
            return_offsets_mapping=True,
            padding=True,
            truncation=True,
        )
        labels = self.align_labels(tag2id, tags, encodings)
        encodings.pop("offset_mapping")  # we don't want to pass this to the model
        dataset = VPDataset(encodings, labels)
        return texts, tags, encodings, labels, dataset


def train_module(
    data_dir, model_dir, train_dataset, eval_dataset, label_list, model_type
):
    def compute_metrics(p, return_entity_level_metrics=True):
        predictions, labels = p
        predictions = np.argmax(predictions, axis=2)

        # Remove ignored index (special tokens); does NOT filter out the I-<tag> labels
        # but just any trailing non-labels due to tokenization
        true_predictions = [
            [label_list[p] for (p, l) in zip(prediction, label) if l != -100]
            for prediction, label in zip(predictions, labels)
        ]
        true_labels = [
            [label_list[l] for (p, l) in zip(prediction, label) if l != -100]
            for prediction, label in zip(predictions, labels)
        ]

        metric = datasets.load_metric("seqeval")
        results = metric.compute(predictions=true_predictions, references=true_labels)
        if return_entity_level_metrics:
            # Unpack nested dictionaries
            final_results = {}
            for key, value in results.items():
                if isinstance(value, dict):
                    for n, v in value.items():
                        final_results[f"{key}_{n}"] = v
                else:
                    final_results[key] = value
            return final_results
        else:
            return {
                "precision": results["overall_precision"],
                "recall": results["overall_recall"],
                "f1": results["overall_f1"],
                "accuracy": results["overall_accuracy"],
            }

    model = DebertaForTokenClassification.from_pretrained(
        f"microsoft/{model_type}", num_labels=len(label_list)
    )

    logging_steps = 50
    saving_steps = 500
    eval_steps = 50
    accum_steps = 1
    warmup_steps = 50
    if "large" in model_type:
        num_epochs = 50
    elif "base" in model_type:
        num_epochs = 10

    # Training
    training_args = TrainingArguments(
        output_dir=model_dir,  # output directory
        overwrite_output_dir=True,
        num_train_epochs=num_epochs,  # total number of training epochs
        per_device_train_batch_size=4,  # batch size per device during training 16
        per_device_eval_batch_size=64,  # batch size for evaluation
        warmup_steps=warmup_steps,  # number of warmup steps for learning rate scheduler
        weight_decay=0.01,  # strength of weight decay
        logging_dir=f"{model_dir}/hf-logs",  # directory for storing logs
        logging_first_step=True,
        logging_steps=logging_steps,
        eval_steps=eval_steps,
        logging_strategy="steps",
        save_strategy="steps",
        save_steps=saving_steps,
        save_total_limit=5,
        evaluation_strategy="steps",
        gradient_accumulation_steps=accum_steps,
        log_level="info",
        load_best_model_at_end=True,
        metric_for_best_model="eval_overall_f1",
        greater_is_better=True,
        report_to="none",
        do_train=True,
        do_eval=True,
    )

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")

    # Set seed before initializing model.
    set_seed(training_args.seed)

    trainer = Trainer(
        model=model,  # the instantiated 🤗 Transformers model to be trained
        args=training_args,  # training arguments, defined above
        train_dataset=train_dataset,  # training dataset
        eval_dataset=eval_dataset,  # evaluation dataset
        compute_metrics=compute_metrics,
    )

    # Detecting last checkpoint.
    if (
        os.path.isdir(training_args.output_dir)
        and training_args.do_train
        and not training_args.overwrite_output_dir
    ):
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif (
            last_checkpoint is not None and training_args.resume_from_checkpoint is None
        ):
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )
    else:
        last_checkpoint = None

    # Training
    if training_args.do_train:
        if training_args.resume_from_checkpoint is not None:
            checkpoint = training_args.resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint
        else:
            checkpoint = None
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        metrics = train_result.metrics
        trainer.save_model()  # Saves the tokenizer too for easy upload
        metrics["train_samples"] = len(train_dataset)
        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()

        # Saving the best checkpoint in ./best-checkpoint directory
        best_model_ckpt_dir = trainer.state.best_model_checkpoint
        save_dir = Path(best_model_ckpt_dir).parent / "best-checkpoint"
        os.rename(best_model_ckpt_dir, save_dir.as_posix())

    # Evaluation
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        metrics = trainer.evaluate()
        metrics["eval_samples"] = len(eval_dataset)
        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)


class Eval:
    def __init__(self, data_dir, model_dir, train_label, eval_label, eval_asr=False):
        """
        Inference with batch size = 1
        """
        self.data_dir = data_dir
        self.model_dir = Path(model_dir).resolve()
        best_model_ckpt_dir = os.path.join(self.model_dir, "best-checkpoint")
        self.model = DebertaForTokenClassification.from_pretrained(
            best_model_ckpt_dir, output_loading_info=False
        )
        self.device = (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )
        self.model.to(self.device)
        self.model.eval()

        self.eval_asr = eval_asr
        self.eval_label = eval_label
        self.train_label = train_label
        self.label_list = read_lst(
            os.path.join(self.data_dir, f"{self.eval_label}_tag_lst_ordered")
        )

    def reduce(self, entity_name):
        return entity_name.split("-")[-1]

    def update_entity_lst(self, lst, entity_name, score_type, entity_info):
        """
        entity_info: word segment when eval_asr is True and word location otherwise
        """
        if self.eval_asr:
            if score_type == "standard":
                lst.append((self.reduce(entity_name), " ".join(entity_info)))
            elif score_type == "label":
                lst.append((self.reduce(entity_name), "word"))
        else:
            if score_type == "standard":
                lst.append((self.reduce(entity_name), entity_info[0], entity_info[-1]))
            elif score_type == "label":
                lst.append((self.reduce(entity_name), 0, 0))

    def make_distinct(self, tag_lst):
        """
        Make enities disticnt in a list
        For instance, when eval_asr == True
        input: [('PER', 'MARY'), ('LOC', "SAINT PAUL'S"), ('PER', 'KIRKLEATHAM'), ('PER', 'MARY')]
        output: [('PER', 'MARY', 1), ('LOC', "SAINT PAUL'S", 1), ('PER', 'KIRKLEATHAM', 1), ('PER', 'MARY', 2)]
        """
        tag2cnt, new_tag_lst = {}, []
        for tag_item in tag_lst[0]:
            _ = tag2cnt.setdefault(tag_item, 0)
            tag2cnt[tag_item] += 1
            if self.eval_asr:
                tag, wrd = tag_item
                new_tag_lst.append((tag, wrd, tag2cnt[tag_item]))
            else:
                tag, _, _ = tag_item
                new_tag_lst.append((tag, 0, tag2cnt[tag_item]))
        return [new_tag_lst]

    def get_entities(self, tag_lst, score_type, text_lst=None):
        """
        Convert entity tag list to the list of (entity-name, location) tuples
        Example:
                >>> seq = ['B-PER', 'I-PER', 'O', 'B-LOC']
                >>> get_entities(seq, "standard")
                [('PER', <word segment>), ('LOC', <word segment>)]
                >>> get_entities(seq, "label")
                [("tag", <word segment>), ("tag", <word segment>)]
        """
        if self.eval_asr:
            assert text_lst is not None
        entity_tag_lst = []
        entity_flag, entity_info, entity_lst, entity_name, prev_tag = (
            False,
            [],
            [],
            None,
            "O",
        )
        for tag_idx, tag_name in enumerate(tag_lst):
            if tag_name != "O":
                if "B-" in tag_name or (
                    "I-" in tag_name and self.reduce(tag_name) != self.reduce(prev_tag)
                ):  # start of a new entity
                    if entity_flag:  # record the previous entity first
                        self.update_entity_lst(
                            entity_lst, entity_name, score_type, entity_info
                        )
                    entity_name = tag_name
                    if self.eval_asr:
                        entity_info = [text_lst[tag_idx]]
                    else:
                        entity_info = [tag_idx]
                    entity_flag = True
                else:  # if "I-" in tag_name and reduce(tag_name) == reduce(prev_tag): # continuation of the entity
                    assert self.reduce(entity_name) == self.reduce(tag_name)
                    assert entity_flag
                    if self.eval_asr:
                        entity_info.append(text_lst[tag_idx])
                    else:
                        entity_info.append(tag_idx)
            else:
                if entity_flag:
                    self.update_entity_lst(
                        entity_lst, entity_name, score_type, entity_info
                    )
                entity_loc = []
                entity_flag = False
                entity_name = None
            if tag_idx == len(tag_lst) - 1:
                if entity_flag:
                    self.update_entity_lst(
                        entity_lst, entity_name, score_type, entity_info
                    )
            prev_tag = tag_name
        entity_tag_lst.append(entity_lst)
        if score_type == "label" or self.eval_asr:
            return self.make_distinct(entity_tag_lst)
        else:
            return entity_tag_lst

    def get_tag_map(self, indices=False, tag_names=False):
        """
        Mapping raw tag ids to the combined tag ids
        """
        assert indices or tag_names
        assert not (indices and tag_names)
        if indices:
            id2tag_raw = load_pkl(os.path.join(self.data_dir, "raw_id2tag.pkl"))
            tag2id_raw = load_pkl(os.path.join(self.data_dir, "raw_tag2id.pkl"))
            id2tag_combined = load_pkl(
                os.path.join(self.data_dir, "combined_id2tag.pkl")
            )
            tag2id_combined = load_pkl(
                os.path.join(self.data_dir, "combined_tag2id.pkl")
            )
            raw_to_combined_id = {}
            for key, value in raw_to_combined_tag_map.items():
                for pfx in ["B-", "I-"]:
                    raw_id = tag2id_raw[pfx + key]
                    if value != "DISCARD":
                        combined_id = tag2id_combined[pfx + value]
                    else:
                        combined_id = tag2id_combined["O"]
                    assert raw_id not in raw_to_combined_id
                    raw_to_combined_id[raw_id] = combined_id
            raw_to_combined_id[tag2id_raw["O"]] = tag2id_combined["O"]
            raw_to_combined_id[-100] = -100
            return raw_to_combined_id
        elif tag_names:
            tag_map_dct = {"O": "O"}
            for key, value in raw_to_combined_tag_map.items():
                for pfx in ["B-", "I-"]:
                    if value != "DISCARD":
                        tag_map_dct[pfx + key] = pfx + value
                    else:
                        tag_map_dct[pfx + key] = "O"
            return tag_map_dct

    def get_entity_tags(
        self,
        predictions,
        labels,
        score_type,
        gt_text=None,
        gt_tags=None,
        pred_text=None,
    ):
        if "combined" in self.eval_label and "raw" in self.train_label:
            tag_map_dct = self.get_tag_map(indices=True)
            predictions = [
                [tag_map_dct[item] for item in prediction] for prediction in predictions
            ]
            labels = [[tag_map_dct[item] for item in label] for label in labels]
        entity_predictions = [
            [self.label_list[p] for (p, l) in zip(prediction, label) if l != -100]
            for prediction, label in zip(predictions, labels)
        ]
        entity_labels = [
            [self.label_list[l] for (p, l) in zip(prediction, label) if l != -100]
            for prediction, label in zip(predictions, labels)
        ]

        entity_predictions_reformat = self.get_entities(
            entity_predictions[0], score_type, pred_text
        )
        if self.eval_asr:
            entity_labels_reformat = self.get_entities(gt_tags, score_type, gt_text)
        else:
            entity_labels_reformat = self.get_entities(entity_labels[0], score_type)
        assert len(entity_labels_reformat[0]) == len(set(entity_labels_reformat[0]))
        assert len(entity_predictions_reformat[0]) == len(
            set(entity_predictions_reformat[0])
        )

        return entity_predictions_reformat, entity_labels_reformat

    def run_inference(
        self,
        score_type,
        eval_dataset_pred,
        eval_texts_gt,
        eval_tags_gt=None,
        eval_texts_pred=None,
    ):
        all_labels = []
        all_predictions = []
        if "combined" in self.eval_label:
            tag_map_dct = self.get_tag_map(tag_names=True)
        data_loader = DataLoader(eval_dataset_pred, batch_size=1, shuffle=False)
        for idx, batch in enumerate(data_loader):
            input_ids = batch["input_ids"].to(self.device)
            attention_mask = batch["attention_mask"].to(self.device)
            labels = batch["labels"].detach().numpy()
            outputs = self.model(input_ids, attention_mask=attention_mask)
            predictions = np.argmax(outputs.logits.cpu().detach().numpy(), axis=2)
            if self.eval_asr:
                if "combined" in self.eval_label:
                    eval_tags_text = [tag_map_dct[item] for item in eval_tags_gt[idx]]
                else:
                    eval_tags_text = eval_tags_gt[idx]
                entity_predictions, entity_labels = self.get_entity_tags(
                    predictions,
                    labels,
                    score_type,
                    eval_texts_gt[idx],
                    eval_tags_text,
                    eval_texts_pred[idx],
                )
            else:
                entity_predictions, entity_labels = self.get_entity_tags(
                    predictions, labels, score_type
                )
            all_labels.extend(entity_labels)
            all_predictions.extend(entity_predictions)

        return all_labels, all_predictions

    def get_scores(
        self,
        score_type,
        eval_dataset_pred,
        eval_texts_gt,
        eval_tags_gt=None,
        eval_texts_pred=None,
    ):
        all_gt, all_predictions = self.run_inference(
            score_type, eval_dataset_pred, eval_texts_gt, eval_tags_gt, eval_texts_pred
        )

        metrics_dct = eval_utils.get_ner_scores(all_gt, all_predictions)
        print(
            "[micro-averaged F1] Precision: %.4f, recall: %.4f, fscore = %.4f"
            % (
                metrics_dct["overall_micro"]["precision"],
                metrics_dct["overall_micro"]["recall"],
                metrics_dct["overall_micro"]["fscore"],
            )
        )

        if score_type == "standard":  # with standard evaluation only
            analysis_examples_dct = eval_utils.ner_error_analysis(
                all_gt, all_predictions, eval_texts_gt
            )
        else:
            analysis_examples_dct = {}

        return metrics_dct, analysis_examples_dct
