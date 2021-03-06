'''
 @Date  : 03/17/2020
 @Author: Zhihan Zhang
 @mail  : zhangzhihan@pku.edu.cn
 @homepage: ytyz1307zzh.github.io
 Extract nouns and verbs for data files generated by prepare_wiki_finetune.py
'''

import json
import argparse
from nltk import pos_tag, word_tokenize
from transformers import BertTokenizer
from typing import List
from tqdm import tqdm


parser = argparse.ArgumentParser()
parser.add_argument('-train_file', type=str, required=True)
parser.add_argument('-eval_file', type=str, required=True)
opt = parser.parse_args()
tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')


def parse_text(sequence_list: List[str], outf_path: str):
    outf = open(outf_path, 'w', encoding='utf-8')
    total_tokens = 0
    total_noun_verb = 0

    for sentence in tqdm(sequence_list):
        tokens = tokenizer.tokenize(sentence)
        pos_list = pos_tag(tokens, tagset='universal')
        word_pos = [str(idx) for idx in range(len(pos_list)) if pos_list[idx][1] in ['NOUN', 'VERB']]
        outf.write(' '.join(word_pos) + '\n')
        total_tokens += len(tokens)
        total_noun_verb += len(word_pos)

    print(f'Total tokens: {total_tokens}, total nouns + verbs: {total_noun_verb} ({total_noun_verb/total_tokens*100:.2f}%)')
    outf.close()


def main():
    """
    The POS results will be saved in the same directory with opt.train_file and opt.eval_file.
    """
    assert opt.train_file.endswith('.txt'), opt.eval_file.endswith('.txt')
    train_outf = opt.train_file[:-4] + '.pos'
    eval_outf = opt.eval_file[:-4] + '.pos'

    with open(opt.train_file, 'r', encoding='utf-8') as f:
        train_lines = [line for line in f.read().splitlines() if (len(line) > 0 and not line.isspace())]
    with open(opt.eval_file, 'r', encoding="utf-8") as f:
        eval_lines = [line for line in f.read().splitlines() if (len(line) > 0 and not line.isspace())]

    print('TRAIN')
    parse_text(train_lines, train_outf)
    print('EVAL')
    parse_text(eval_lines, eval_outf)


if __name__ == '__main__':
    main()
