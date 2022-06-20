asr_model_type=$1 #w2v2-base
ner_model_type=$2 #deberta-base
eval_set=$3 #dev
train_label=$4 #raw
eval_label=$5 #combined
lm=$6 #nolm

model_ckpt=`realpath save/asr/${asr_model_type}-vp`
stage=-1

set -euo pipefail

if [ $stage -le -1 ]; then
    python slue_toolkit/eval/eval_w2v.py eval_asr \
        --model $model_ckpt \
        --data manifest/slue-voxpopuli \
        --subset ${eval_set} \
        --lm ${lm}
fi

if [ $stage -le 0 ]; then
    # Add post processing script
    python slue_toolkit/text_ner/reformat_pipeline.py prep_data \
        --model_type ${asr_model_type} \
        --asr_data_dir manifest/slue-voxpopuli \
        --asr_model_dir save/asr/${asr_model_type}-vp \
        --out_data_dir manifest/slue-voxpopuli/nlp_ner \
        --eval_set $eval_set \
        --lm $lm
fi


if [ $stage -le 1 ]; then
    python slue_toolkit/text_ner/ner_deberta.py eval \
        --data_dir manifest/slue-voxpopuli/nlp_ner \
        --model_dir save/nlp_ner/${ner_model_type}_${train_label} \
        --model_type $ner_model_type \
        --eval_asr True \
        --train_label $train_label \
        --eval_label $eval_label \
        --eval_subset $eval_set \
        --lm $lm \
        --asr_model_type $asr_model_type \
        --save_results True  
fi
