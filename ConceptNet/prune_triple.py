'''
 @Date  : 01/03/2020
 @Author: Zhihan Zhang
 @mail  : zhangzhihan@pku.edu.cn
 @homepage: ytyz1307zzh.github.io
'''
import json
from tqdm import tqdm
import argparse
import re
from typing import List, Dict, Set
# import nltk
# nltk.download('stopwords')
# nltk_stopwords = nltk.corpus.stopwords.words('english')
# nltk_stopwords += ["like", "gone", "did", "going", "would", "could", "get", "in", "up", "may"]
from spacy.lang.en import STOP_WORDS
from Stemmer import PorterStemmer
stemmer = PorterStemmer()


def stem(word: str) -> str:
    """
    Stem a single word
    """
    word = word.lower().strip()
    return stemmer.stem(word)


def get_weight(line: str) -> float:
    triple = line.strip().split(', ')
    assert len(triple) == 9
    weight = float(triple[-2])
    return weight


def get_relation(line: str) -> str:
    triple = line.strip().split(', ')
    assert len(triple) == 9
    relation = triple[0]
    return relation


def read_relation(filename: str) -> Dict[str, str]:
    file = open(filename, 'r', encoding='utf8')
    rel_rules = {}

    for line in file:
        rule = line.strip().split(': ')
        relation, direction = rule
        rel_rules[relation] = direction

    return rel_rules


def extract_context(paragraph) -> Set[str]:
    """
    Acquire all content words from a paragraph.
    """
    paragraph = paragraph.lower().strip().split()
    return {word for word in paragraph if word not in STOP_WORDS and word.isalpha()}


def valid_direction(relation: str, direction: str, rel_rules: Dict[str, str]) -> bool:
    """
    Check the semantic role of the entity (subj or obj) is valid or not, according to the rel_rules
    """
    assert direction in ['left', 'right']
    rule = rel_rules[relation]
    assert rule in ['left', 'right', 'both']

    if rule == 'both':
        return True
    elif rule == direction:
        return True
    else:
        return False


def in_context(concept: Set, context: Set) -> bool:
    """
    Score: (words in both concept and context) / (words in concept)
    """
    score = len(concept.intersection(context)) / len(concept)
    return score >= 0.5


def select_triple(entity: str, raw_triples: List[str], context_set: Set[str], rel_rules: Dict[str, str], max: int) -> List[str]:
    """
    Select related triples from the rough retrieval set.
    Args:
        entity - entity name, may contain a semicolon delimiter.
        context_set - the content words in the context (paragraph + topic)
        rel_rules - selection rules for each relation type (subj only, or both subj and obj?)
    """
    triples_by_score = []
    triples_by_relevance = []

    entity_list = entity.split(';')
    entity_set = set()
    for ent in entity_list:
        entity_set = entity_set.union(set(map(stem, ent.split())))

    stem_context = set(map(stem, context_set)) - entity_set

    for line in raw_triples:

        triple = line.strip().split(', ')
        assert len(triple) == 9

        direction = triple[-1]  # LEFT or RIGHT
        relation = triple[0]  # relation type

        # if the semantic role of the entity (subj or obj) does not match, skip this
        if not valid_direction(relation = relation, direction = direction.lower(), rel_rules = rel_rules):
            continue

        triples_by_score.append(line)

        # find the neighbor concept
        if direction == 'LEFT':
            neighbor = set(triple[4].strip().split('_'))
        elif direction == 'RIGHT':
            neighbor = set(triple[1].strip().split('_'))

        if in_context(concept = neighbor, context = stem_context):
            triples_by_relevance.append(line)

    triples_by_relevance = [t for t in triples_by_relevance if get_weight(t) >= 1.0]
    if len(triples_by_relevance) > (max//2):
        triples_by_relevance = sorted(triples_by_relevance, key=lambda x: get_relation(x) != 'relatedto', reverse=True)
        triples_by_relevance = sorted(triples_by_relevance, key=get_weight, reverse=True)
        triples_by_relevance = triples_by_relevance[:max//2]

    triples_by_score = sorted(triples_by_score, key = lambda x: get_relation(x) != 'relatedto', reverse = True)
    triples_by_score = sorted(triples_by_score, key = get_weight, reverse = True)
    triples_by_score = triples_by_score[:(max - len(triples_by_relevance))]
    print('relevance: ', len(triples_by_relevance), end=', ')
    print('score: ', len(triples_by_score))

    return triples_by_relevance + triples_by_score


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-input', type=str, default='./rough_retrieval.txt', help='path to the english conceptnet')
    parser.add_argument('-output', type=str, default='./retrieval.txt', help='path to store the generated graph')
    parser.add_argument('-relation', type=str, default='./relation_direction.txt', help='path to the relation rules')
    parser.add_argument('-max', type=int, default=20, help='how many triples to collect')
    opt = parser.parse_args()

    data = json.load(open(opt.input, 'r', encoding='utf8'))
    rel_rules = read_relation(opt.relation)
    result = []

    for instance in data:
        para_id = instance['id']
        entity = instance['entity']
        paragraph = instance['paragraph']
        topic = instance['topic']
        raw_triples = instance['cpnet']

        context_set = extract_context(paragraph)
        context_set.union(extract_context(topic))

        selected_triples = select_triple(entity = entity, raw_triples = raw_triples, context_set = context_set,
                                         rel_rules = rel_rules, max = opt.max)
        print(f'Triples before selection: {len(raw_triples)}, after selection: {len(selected_triples)}')

        result.append({'id': para_id,
                     'entity': entity,
                     'topic': topic,
                     'paragraph': paragraph,
                     'cpnet': selected_triples
                     })

    json.dump(result, open(opt.output, 'w', encoding='utf-8'), indent=4, ensure_ascii=False)
    print(f'{len(result)} instances finished.')