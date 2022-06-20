model_type=$1 #deberta-base
eval_set=$2 #dev
train_label=$3 #raw
eval_label=$4 #combined

python slue_toolkit/text_ner/ner_deberta.py eval \
--data_dir manifest/slue-voxpopuli/nlp_ner \
--model_dir save/nlp_ner/${model_type}_${train_label} \
--model_type $model_type \
--eval_asr False \
--train_label $train_label \
--eval_label $eval_label \
--eval_subset $eval_set \
--save_results True 
