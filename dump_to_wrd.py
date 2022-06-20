import os
import shutil

PREDICT_PATH = './preds/vp/w2v2-base-vp'
OUTPUT_PATH  = './manifest/slue-voxpopuli'

def tsv_to_wrd(path, asr_tag):
    # read tsv file
    pred_datas = None
    with open(path, 'r', encoding='utf-8') as fr:
        pred_datas = fr.read()
    pred_datas = pred_datas.split('\n')[1:-1]
    
    # get pred_text
    pred_texts = []
    for pred in pred_datas:
        pred_texts.append(pred.split('\t')[-1])

    # create folder
    output_folder = os.path.join(OUTPUT_PATH, asr_tag)
    if not os.path.exists(output_folder):
        os.mkdir(output_folder)
    output_file = ('_{}_'.format(asr_tag)).join((path.split('/')[-1]).split('.')[:-1])
    split_tag   = (path.split('/')[-1]).split('.')[0]

    # copy .tsv .sent
    subs = ['.tsv']
    for sub in subs:
        src = os.path.join(OUTPUT_PATH, split_tag + sub)
        dst = os.path.join(output_folder, output_file  + sub)
        shutil.copyfile(src, dst)

    # create wrd
    output_file_path = os.path.join(output_folder, output_file + '.wrd')
    with open(output_file_path, 'w', encoding='utf-8') as fr:
        fr.write('\n'.join(pred_texts) + '\n')

    return

if __name__ == '__main__':
    asr_tag   = (PREDICT_PATH.split('/')[-1])[:-2] + PREDICT_PATH.split('/')[-2]

    for pred_file in os.listdir(PREDICT_PATH):
        pred_path  = os.path.join(PREDICT_PATH, pred_file)
        tsv_to_wrd(pred_path, asr_tag)

