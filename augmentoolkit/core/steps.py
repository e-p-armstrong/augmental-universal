import os
import json
import re
import sys
from tqdm import asyncio as tqdmasyncio
from augmentoolkit.generation_functions.engine_wrapper_class import EngineWrapper
from augmentoolkit.generation_functions.pipeline_step_class import PipelineStep
from augmentoolkit.utils.make_id import make_id
from augmentoolkit.utils.write_output_to_file import write_output_to_file
from augmentoolkit.generation_functions.safe_formatter import safe_format
from nltk.tokenize import sent_tokenize
import matplotlib.pyplot as plt
from collections import Counter
import logging
from math import ceil
import traceback
import glob
import yaml
from datasets import load_dataset


from augmentoolkit.utils.create_conv_starter import create_conv_starter
from augmentoolkit.utils.extract_steps import extract_steps
from augmentoolkit.utils.escape_unescaped_quotes import escape_unescaped_quotes

from augmentoolkit.generation_functions import (
    extract_question_answer,
    process_multiturn_functions,
)
from augmentoolkit.generation_functions.format_qatuples import format_qatuples

from augmentoolkit.generation_functions.generation_step_class import GenerationStep
from augmentoolkit.generation_functions.special_instructions import special_instructions

with open("./config.yaml", "r") as file:
    obj_conf = yaml.safe_load(file)

DEFAULT_PROMPT_PATH = obj_conf["PATH"]["DEFAULT_PROMPTS"]
HUB_PATH = obj_conf["HUGGINGFACE"]["HUB_PATH"]
PRIVATE = obj_conf["HUGGINGFACE"]["PRIVATE"]
PUSH_TO_HUB = obj_conf["HUGGINGFACE"]["PUSH_TO_HUB"]
USE_FILENAMES = obj_conf["SYSTEM"]["USE_FILENAMES"]
OUTPUT_DIR = obj_conf["PATH"]["OUTPUT"]
PROMPTS_DIR = obj_conf["PATH"]["PROMPTS"]
DEFAULT_PROMPTS = obj_conf["PATH"]["DEFAULT_PROMPTS"]
USE_STOP = obj_conf["SYSTEM"]["STOP"]
COMPLETION_MODE = obj_conf["SYSTEM"]["COMPLETION_MODE"]

has_pushed_yet = False

def extract_qa_tuples(text):
    pattern = r"\*\*QUESTION:\*\*\s*((?:.|\n)*?)\s*\*\*ANSWER:\*\*\s*((?:.|\n)*?)(?=\s*\*\*QUESTION:\*\*|\Z)"
    matches = re.findall(
        pattern, text + "\n\n**QUESTION:**", re.DOTALL
    )  # The addition is a hack to get around the tricky lookahead problem
    return [(question.strip(), answer.strip()) for question, answer in matches]

import os


# Also used basically everywhere:
def convert_logging_to_dataset(directory):
    print("entering saving mode")
    global has_pushed_yet
    
    output_dir = os.path.join(obj_conf["PATH"]["OUTPUT"], directory)
    
    output_file_path = os.path.join(obj_conf["PATH"]["OUTPUT"], directory + "_DATAGEN_OUTPUT.jsonl")
    
    if not os.path.exists(output_dir):
        raise Exception("ERROR!! Trying to convert a logging directory to a dataset, when that directory does not exist!")
        
    full_list_of_dicts = []
    with open(output_file_path, "w") as f:
        existing_files = glob.glob(
            os.path.join(output_dir, "*.yaml")
        )
        
        for file in existing_files:
            with open(file,'r') as file2:
                file_list_of_dicts = yaml.safe_load(file2)
            # print(file_list_of_dicts)
            
            sysprompt = {"from": "system", "value": file_list_of_dicts[0]["content"]}
            input = {"from": "human", "value": file_list_of_dicts[-2]["content"]}
            output = {"from": "gpt", "value": file_list_of_dicts[-1]["content"]}
            
            json_to_write = {"conversations": [sysprompt, input, output]}
            
            f.write(json.dumps(json_to_write) + "\n")
            full_list_of_dicts.append(json_to_write)
    print("...Converted successfully (we think)")
    
    dataset_with_split_output_file_path = os.path.join(obj_conf["PATH"]["OUTPUT"], directory + "_DATAGEN_OUTPUT_SPLIT.json")
    with open(dataset_with_split_output_file_path, "w") as f:
            json_to_write = {"train": full_list_of_dicts}
            
            f.write(json.dumps(json_to_write) + "\n")
            
    
    if PUSH_TO_HUB:
        if os.path.exists(output_file_path):
            dataset = load_dataset("json", data_files=dataset_with_split_output_file_path,  split="train")
            print("DATASET TYPE:")
            print(type(dataset))
            part_nb = directory.split("_")[0]
            if not has_pushed_yet:
                    dataset.push_to_hub(HUB_PATH, private=PRIVATE)
                    dataset.to_parquet(f"hf://datasets/{HUB_PATH}/train{part_nb}.parquet")
                    has_pushed_yet = True
            else:
                dataset.to_parquet(f"hf://datasets/{HUB_PATH}/train-{part_nb}.parquet")
    # remove the output with split file
    os.remove(dataset_with_split_output_file_path)
    
    
    
    
    
    
def convert_revised_questions_to_question_generation_training(qa_tuples_by_paragraph, use_filenames):
    print("entering saving mode")
    # found a solution to overfitting on the examples:
    # TRAIN WITHOUT THEM
    # This will produce a WEALTH of instruct data
    # fucking awesome, hopefully
    # also it's also about the domain, lmao
    # so more domain knowledge
    
    output_file_path = os.path.join(obj_conf["PATH"]["OUTPUT"], "questions_generation_dataset.jsonl")
    
    if use_filenames:
        question_generation_prompt = os.path.join(obj_conf["PATH"]["PROMPTS"], "qatuples_gen_filenames.yaml")
    else:
        question_generation_prompt = os.path.join(obj_conf["PATH"]["PROMPTS"], "qatuples_gen_no_filenames.yaml")

    with open(question_generation_prompt, "r") as f:
        qgen_prompt_full = yaml.safe_load(f)
        
        sysprompt = qgen_prompt_full[0]["content"]
        input_template = qgen_prompt_full[-1]["content"]
    
    # revised_questions_output_path = os.path.join(obj_conf["PATH"]["OUTPUT"], "qatuples_revised")
    convos = []
    with open(output_file_path, 'w') as out_file:
        for qatup_group in qa_tuples_by_paragraph:
            answer = format_qatuples(qatup_group)
            text = qatup_group[0][2]
            
            # print(text)
            if not use_filenames:
                input_text = safe_format(input_template, text=text)
            else:
                textname = qatup_group[0][3]
                input_text = safe_format(input_template, text=text, textname=textname)
            sysprompt_obj = {"from": "system", "value": sysprompt}
            input_obj = {"from": "human", "value": input_text}
            answer_obj = {"from": "gpt", "value": answer}
            
            convo = {"conversations": [sysprompt_obj, input_obj, answer_obj]}
            out_file.write(json.dumps(convo) + "\n")
            convos.append(convo)

    print("...Converted successfully (we think)")
    if PUSH_TO_HUB: ## IMPORTANT STUFF FOR YOU BEGINS HERE ##
        # temporarily create a json file with splits to load the dataset from
        output_file_path = os.path.join(obj_conf["PATH"]["OUTPUT"], "questions_generation_dataset_split.json")
        with open(output_file_path, 'w') as out_file_json:
            json.dump({"train": convos},out_file_json)
        dataset = load_dataset("json", data_files=output_file_path, split="train") # THIS APPROACH WORKS!
        
        with open(output_file_path[:-1], 'w') as out_file_json:
            json.dump(convo,out_file_json)
        dataset.to_parquet(f"hf://datasets/{HUB_PATH}/data/train-qgen.parquet")
        os.remove(output_file_path)
    
    
    
    

def extract_reasoning_from_context_check(response):
    # print("\n----\/----\n RESPONSE:")
    # print(response)
    # print("\n\n\n---/\---\n\n")
    decision_pattern = re.compile(r"Final judgment:(.+)", re.IGNORECASE)
    determination = decision_pattern.search(response)
    if determination:
        determination = determination.group(1).strip()
    if not determination:
        print("LLM ISSUE: Did not contain a determination! Maybe check your LLM it is being stupid, or perhaps the input is diffuclt.")
        return None, response
    if "PASS" in determination:
        print("Leaving be...")
        return (True, response)  # , completion
    elif "REWORD" in determination:
        print("Rewording...")
        q, a = extract_question_answer.extract_question_answer(response)
        print((q, a))
        if "the provided" in a.lower(): # catch infrequent cases where the reworded answer contains reference to provided information
            print("'The provided' found in reworded answer -- Setting to None...")
            return (False, response)
        if "the reworded" in a.lower(): # Catch infrequent cases where it talks about the reworded question and answer pair
            print("'The reworded' found in reworded answer -- Setting to None...")
            return (False, response)
        if "mention" in a.lower():
            print("'Mention' found in reworded answer -- Setting to None...")
            return (False, response)
        if "no information" in a.lower():
            print("'No information' found in reworded answer -- Setting to None...")
            return (False, response)
        if "follow the instructions in a separate" in a.lower():
            print("'Follow the instructions in a separate' found in reworded answer -- Setting to None...")
            return (False, response)
        return (q, a)  # (q, a, qatuple[2], qatuple[3]), completion
    elif "FAIL" in determination:
        print("Setting to None...")
        return (False, response)  # , completion
    else:
        print("Did not contain relevant or irrelevant! Retrying")
        raise Exception("error in judgement extraction (ans relevancy)")

### CONTEXT REPAIR SECTION

context_repairer_path = "check_qatuple_context_no_filenames"
if USE_FILENAMES:
    context_repairer_path = "check_qatuple_context_filenames"


repair_context_regex = re.compile(
        r"Reasoning and thought process \(be thorough\):(.+)",
        re.DOTALL | re.IGNORECASE,
    )

class ContextRepairer(PipelineStep):
    def __init__(self):
        super().__init__(
            prompt_folder=PROMPTS_DIR,
            default_prompt_folder=DEFAULT_PROMPTS,
            prompt_path=context_repairer_path,
            regex=repair_context_regex,
            sampling_params={
                "max_tokens": 2000,
                "stop": [
                    "### Response",
                    "\n\n\n\n\n\n\n\n\n\n\n\n\n",
                    "</s>",
                    "# Input:",
                    "[INST]",
                    "### Instruction",
                    "[INST",
                    "<|eot_id|>",
                    "<|start_header_id|>",
                    "<|end_header_id|>",
                ],
                "temperature": 0.2,
            },
            output_dir=OUTPUT_DIR,
            output_subdir="question_context_revision_generations",
            intermediate_output_path="revised_qatuples_intermediates",
            save_path="revised_qatuples_saved",
            output_processor=extract_reasoning_from_context_check,
            result_key="not gonna be used", # we do not employ the result key because we replace the question and answer in the qa dict.
            use_stop=USE_STOP,
            completion_mode=COMPLETION_MODE,
            
        )
        
    def read_previous_output(self, idx, output_list):
        save_path_file = self.make_save_path_file(idx)
        
        if os.path.exists(save_path_file):
            with open(save_path_file, "r") as f:
                content = f.read()  # Read the file once and store its content
                print(save_path_file)
                if content == "failed":
                    print("Loaded failed file")
                    output_list[idx] = None
                    return True
                print("Loaded file:")
                print(content)
                try:
                    data = json.loads(content)  # Convert the string back to JSON
                    output_list[idx] = data
                    return True
                except json.JSONDecodeError:
                    print("JSON decode error with the contents:", content)
        return False
    
    def save(self, result=None, full_output=None, idx=None, output_list=None, input_data=None):
        if isinstance(result[0], str):
            new_question = result[0]
            new_answer = result[1]
            
            output_list[idx]['question'] = new_question
            output_list[idx]['answer'] = new_answer
        elif not result[0]:
            output_list[idx] = None
        
        id = make_id()
        write_output_to_file(full_output, self.intermediate_output_path_full, id)
        
        os.makedirs(self.save_path_dir, exist_ok=True)
        if output_list[idx]:
            with open(self.make_save_path_file(idx), "w") as f:
                f.write(json.dumps(output_list[idx]))
        else:
            with open(self.make_save_path_file(idx), "w") as f:
                f.write("failed")
    
context_repairer = ContextRepairer()

# Postprocessing function for question/answer validation
async def repair_qatuple_context(
    idx,
    dict,
    engine_wrapper,
    vetted_qa_dicts,
):
    await context_repairer.run(idx, dict, engine_wrapper, output_list=vetted_qa_dicts)


def parse_answer_accuracy_validation(response):
    determination_pattern = re.compile(
        r"Overall Accuracy Determination:(.+)", re.DOTALL
    )
    try:
        determination = determination_pattern.search(response).group(1).strip()
    except Exception as e:
        print("Error encountered, model messed up output format")
        print(e)
        return (False, response)
    if (
        "inaccurate" in determination.lower()
        or "Inaccurate" in determination.lower()
        or "mostly" in determination.lower()
        or "partial" in determination.lower()
        or "irrelevant" in determination.lower()
    ):  # The "mostly" is there to catch "mostly accurate" which the model says occasionally, and which actually means inaccurate.
        return (False, response)
    elif "accurate" in determination.lower():
        return (True, response)
    else:
        print("Answer accuracy validation made a mistake")
        raise Exception("answer accuracy validation did not include a judgement")


# Control flow helpers -- Question/Answer Validation
async def vet_answer_accuracy_loop(
    qa_dict,
    run_id,
    engine_wrapper=None,
    double_check_counter=3,
    completion_mode=None,
    logging_level=None,
    file_path=None,
):
    # NOTE Set up answer check generation step
    prompt_path_ans_accuracy_check = "check_answer"
    if completion_mode:
        prompt_path_ans_accuracy_check = prompt_path_ans_accuracy_check + ".txt"
    else:
        prompt_path_ans_accuracy_check = prompt_path_ans_accuracy_check + ".yaml"
    check_ans_accuracy_regex = re.compile(
        r"Reasoning and thought process \(the text is your single source of truth\):\n(.+)",
        re.DOTALL,
    )
    # TODO performance improvement could be gained by using async for to do the checks simultaneously
    answer_accuracy_checker = GenerationStep(
        prompt_path=prompt_path_ans_accuracy_check,
        regex=check_ans_accuracy_regex,
        sampling_params={
            "max_tokens": 1500,
            "stop": [
                "### Response",
                "\n\n\n\n\n",
                "</s>",
                "# Input:",
                "[INST]",
                "### Instruction",
                "[INST",
                "<|eot_id|>",
                "<|start_header_id|>",
                "<|end_header_id|>",
            ],
            "temperature": 0.2,
        },
        completion_mode=completion_mode,
        retries=1,
        engine_wrapper=engine_wrapper,
        logging_level=logging_level,
        output_processor=parse_answer_accuracy_validation,
        prompt_folder=obj_conf["PATH"]["PROMPTS"],
        default_prompt_folder=DEFAULT_PROMPT_PATH,
        use_stop=obj_conf["SYSTEM"]["STOP"],
    )

    # Resume normal control flow code

    try:
        # print(
        # f"\n\nStarting ACCURACY loop for question: {qtuple[0]}, context: {qtuple[2]}"
        # )
        passed_checks = 0
        times_checked = 0
        dissenting_reasoning = ""
        while times_checked < double_check_counter:
            check_id = make_id()
            # print(
            # f"\n\nACCURACY CALL CHECK ANSWER: {qtuple[0]}, context: {qtuple[2]}, retries: {total_retries}, dissenting reasoning: {dissenting_reasoning}"
            # )
            judgement, answer_accuracy_output = await answer_accuracy_checker.generate(
                paragraph=qa_dict["paragraph"],
                question=qa_dict["question"],
                answer=qa_dict["answer"],
            )
            write_output_to_file(
                answer_accuracy_output,
                obj_conf["PATH"]["OUTPUT"] + "/check_answer_accuracy_generations",
                run_id + "--check--" + check_id,
            )
            if not judgement[0]:  # if not accurate
                dissenting_reasoning = judgement[1]
                print("\nNegative Vote Cast! Here was the reasoning:\n")
                print(dissenting_reasoning)
            else:
                passed_checks += 1
            times_checked += 1
            if passed_checks >= ceil(double_check_counter / 2):
                break
            failed_checks = times_checked - passed_checks
            if failed_checks >= ceil(double_check_counter / 2):
                break

        if passed_checks >= ceil(double_check_counter / 2):  # if question checks passed
            # print(f"\n\ANSWER ACCURACY CHECKS PASSED retries: {total_retries}")
            return qa_dict
        else:
            print("Answer accuracy validation failed! Tossing")
            with open(file_path, "w") as file:
                    file.write("failed")
            return
    except Exception as e:
        print("!!ERROR!!")
        print(e)
        traceback.print_exc()

    with open(file_path, "w") as file:
        file.write("failed")
    return


def parse_answer_relevancy_validation_step(thought_process):
    judgement_pattern = re.compile(
        r"Explanation of Judgment:(.+)", re.DOTALL | re.IGNORECASE
    )
    try:
        determination = judgement_pattern.search(thought_process).group(1).strip()
        if (
            "irrelevant" in determination.lower()
            or "mostly" in determination.lower()
            or "partial" in determination.lower()
            or "introduces information not present in the text" in determination.lower()
        ):  # Hack to get around faulty outputs
            return (False, thought_process)  # , completion
        elif "relevant" in determination or "Relevant" in determination:
            return (True, thought_process)  # , completion
        else:
            print(f"Answer relevancy parsing failed! Retrying! {judgement_pattern}")
            raise Exception("error in judgement extranction (ans relevancy)")
    except Exception as e:
        print("Model did not provide a judgement")
        print(e)
        # raise Exception("retry")
        return (False, thought_process)


async def vet_answer_relevance_loop(
    qa_dict,
    run_id,
    engine_wrapper=None,
    double_check_counter=3,
    completion_mode=None,
    logging_level=None,
    file_path=None,
):
    # NOTE Set up answer check generation step
    prompt_path_ans_relevancy_check = "check_answer_relevancy_with_text"
    check_ans_relevancy_regex = re.compile(
        r"Reasoning and thought process \(be careful about extra details, even vague ones\):\n(.+)",
        re.DOTALL | re.IGNORECASE,
    )

    if completion_mode:
        prompt_path_ans_relevancy_check = prompt_path_ans_relevancy_check + ".txt"
    else:
        prompt_path_ans_relevancy_check = prompt_path_ans_relevancy_check + ".yaml"

    answer_relevancy_checker = GenerationStep(
        prompt_path=prompt_path_ans_relevancy_check,
        regex=check_ans_relevancy_regex,
        sampling_params={
            "max_tokens": 1500,
            "stop": [
                "### Response",
                "\n\n\n\n\n\n",
                "</s>",
                "# Input:",
                "[INST]",
                "### Instruction",
                "[INST",
                "<|eot_id|>",
                "<|start_header_id|>",
                "<|end_header_id|>",
            ],
            "temperature": 0.2,
        },
        completion_mode=completion_mode,
        retries=1,
        engine_wrapper=engine_wrapper,
        logging_level=logging_level,
        output_processor=parse_answer_relevancy_validation_step,
        prompt_folder=obj_conf["PATH"]["PROMPTS"],
        default_prompt_folder=DEFAULT_PROMPT_PATH,
        use_stop=obj_conf["SYSTEM"]["STOP"]
    )

    # Resume normal control flow code
    try:
        passed_checks = 0
        times_checked = 0
        dissenting_reasoning = ""
        while times_checked < double_check_counter:
            
            check_id = make_id()
            (
                judgement,
                answer_relevancy_output,
            ) = await answer_relevancy_checker.generate(
                paragraph=qa_dict["paragraph"],
                question=qa_dict["question"],
                answer=qa_dict["answer"],
            )
            write_output_to_file(
                answer_relevancy_output,
                obj_conf["PATH"]["OUTPUT"] + "/check_answer_relevancy_generations",
                check_id,
            )
            if not judgement[0]:  # if not relevant
                dissenting_reasoning = judgement[1]
                print("\nNegative Vote Cast! Here was the reasoning:\n")
                print(dissenting_reasoning)
            else:
                passed_checks += 1
            times_checked += 1
            if passed_checks >= ceil(double_check_counter / 2):
                break
            failed_checks = times_checked - passed_checks
            if failed_checks >= ceil(double_check_counter / 2):
                break

        if passed_checks >= ceil(double_check_counter / 2):  # if question checks passed
            # print(f"\n\ANSWER ACCURACY CHECKS PASSED retries: {total_retries}")
            return await vet_answer_accuracy_loop(
                qa_dict,
                run_id,
                engine_wrapper=engine_wrapper,
                double_check_counter=double_check_counter,
                completion_mode=completion_mode,
                logging_level=logging_level,
                file_path=file_path
            )
        else:
            print("Answer relevancy validation failed! Tossing")
            with open(file_path, "w") as file:
                    file.write("failed")
            return
    except Exception as e:
        print("!!ERROR!!")
        print(e)
        traceback.print_exc()

    with open(file_path, "w") as file:
        file.write("failed")
    return


def parse_validation_step(response):
    # print("!!! RESPONSE !!!")
    # print(response)
    decision_pattern = re.compile(r"Critical Evaluation and Final Judgment:(.+)", re.DOTALL | re.IGNORECASE)
    determination = decision_pattern.search(response).group(1).strip()
    # print("!!! DETERMINATION !!!")
    # print(determination)
    if (
        "irrelevant" in determination
        or "Irrelevant" in determination.lower()
        or "mostly" in determination.lower()
        or "partial" in determination.lower()
        or "introduces information not present in the text" in determination.lower()
    ):
        return (
            False,
            response,
        )  # TODO ensure that in the control flow code it passes on (False, response), completion
    elif "relevant" in determination.lower():
        return (True, response)  # TODO same as above(True, response), completion
    else:
        print("Did not contain relevant or irrelevant! Retrying")
        raise Exception(
            "Validation step screwed up and did not reach a conclusion! Retrying!"
        )


async def vet_question_loop( # NOTE adding the pipelinestep class would make this a bit more complex, rather than less; so this is not refactored to use that class
    qa_dict,
    question_group_id=None,
    engine_wrapper=None,
    qa_dicts_dir=None,
    vetted_qa_dicts=None,
    double_check_counter=3,
    completion_mode=None,
    logging_level=None,
):
    try:
        file_path = os.path.join(qa_dicts_dir, f"para_{qa_dict['paragraph_idx']}_q_{qa_dict['question_idx']}.json")
        
        if os.path.exists(file_path):
            with open(file_path, "r") as file:
                file_body = file.read()
                if file_body == "failed":
                    qa_dict = None
                else:
                    file.seek(0)
                    qa_dict = json.loads(file_body)
            vetted_qa_dicts.append(qa_dict)
            return
        
        # NOTE Set up question check generation step
        prompt_path_q_check = "check_question"
        check_q_regex = re.compile(
            r"Reasoning and thought process \(be careful around \"how\" and \"why\" questions\):(.+)",
            re.DOTALL | re.IGNORECASE,
        )

        if completion_mode:
            prompt_path_q_check = prompt_path_q_check + ".txt"
        else:
            prompt_path_q_check = prompt_path_q_check + ".yaml"

        question_checker = GenerationStep(
            prompt_path=prompt_path_q_check,
            regex=check_q_regex,
            sampling_params={
                "max_tokens": 1500,
                "stop": [
                    "### Response",
                    "\n\n\n\n\n",
                    "</s>",
                    "# Input:",
                    "[INST]",
                    "### Instruction",
                    "[INST",
                    "<|eot_id|>",
                    "<|start_header_id|>",
                    "<|end_header_id|>",
                ],
                "temperature": 0.2,
            },
            completion_mode=completion_mode,
            retries=1,
            engine_wrapper=engine_wrapper,
            logging_level=logging_level,
            output_processor=parse_validation_step,
            prompt_folder=obj_conf["PATH"]["PROMPTS"],
            default_prompt_folder=DEFAULT_PROMPT_PATH,
            use_stop=obj_conf["SYSTEM"]["STOP"],
        )

        # NOTE Set up generate new question step
        # MODIFICATION: so that the conversations make sense, we just toss failed questions, rather than regenning. They're plentiful enough.
        try:
            # print(
            #     f"\n\nStarting QUESTION loop for question: {qtuple[0]}, context: {qtuple[2]}"
            # )
            run_id = question_group_id + "--subquestion--" + make_id()
            passed_checks = 0
            times_checked = 0
            dissenting_reasoning = ""
            if obj_conf["SKIP"]["QUESTION_CHECK"]:
                print("DEBUG: Skipping question check")
                return await vet_answer_accuracy_loop(
                    qa_dict,
                    run_id,
                    engine_wrapper=engine_wrapper,
                    double_check_counter=double_check_counter,
                    completion_mode=completion_mode,
                    logging_level=logging_level,
                    file_path=file_path
                )
            while times_checked < double_check_counter:
                check_id = make_id()
                # print(
                #     f"\n\nQUESTION CALL CHECK ANSWER: {qtuple[0]}, context: {qtuple[2]}, retries: {total_retries}, dissenting reasoning: {dissenting_reasoning}"
                # )
                judgement, check_q_output = await question_checker.generate(paragraph=qa_dict["paragraph"], question=qa_dict["question"], answer=qa_dict["answer"])

                # Now we need to put the judgement together into the format it expects it to be in

                write_output_to_file(
                    check_q_output,
                    obj_conf["PATH"]["OUTPUT"] + "/check_question_generations",
                    run_id + "--check--" + check_id,
                )
                
                # print("JUDGEMENT:")
                # print(judgement)
                if not judgement[0]:  # if not relevant
                    dissenting_reasoning = judgement[1]
                    print("\nNegative Vote Cast! Here was the reasoning:\n")
                    print(dissenting_reasoning)
                    print(f"ID: {check_id}")
                else:
                    passed_checks += 1
                times_checked += 1
                if passed_checks >= ceil(double_check_counter / 2):
                    break
                failed_checks = times_checked - passed_checks
                if failed_checks >= ceil(double_check_counter / 2):
                    break

            if passed_checks >= ceil(
                double_check_counter / 2
            ):  # if all question checks passed
                # print(f"\n\nQUESTION CHECKS PASSED retries: {total_retries}")
                
                if obj_conf["SKIP"]["ANSWER_RELEVANCY_CHECK"]:
                    res = await vet_answer_accuracy_loop(
                        qa_dict,
                        run_id,
                        engine_wrapper=engine_wrapper,
                        double_check_counter=double_check_counter,
                        completion_mode=completion_mode,
                        logging_level=logging_level,
                        file_path=file_path
                    )
                else:
                    res = await vet_answer_relevance_loop(
                        qa_dict,
                        run_id,
                        engine_wrapper=engine_wrapper,
                        double_check_counter=double_check_counter,
                        completion_mode=completion_mode,
                        logging_level=logging_level,
                        file_path=file_path
                    )
                
                # Return response
                
                vetted_qa_dicts.append(res)
                if res is not None:
                    with open(file_path, "w") as file:
                        json.dump(res, file, indent=4)
                return
            else: # this path is probably redundant
                print("Question accuracy validation failed! Tossing")
                with open(file_path, "w") as file:
                    file.write("failed")
                return
        except Exception as e:
            print("!!ERROR!!")
            print(e)
            traceback.print_exc()
        with open(file_path, "w") as file:
            file.write("failed")
    except Exception as e:
        print(f"Q ERROR: {e}")
        traceback.print_exc()




### Question Generation Section

def extract_questions_from_response(
    generation,
):  # TODO extract to non-controlflow file
    questions = extract_qa_tuples(generation)
    if len(questions) == 0:
        print("FAILED TO GENERATE QUESTIONS!")
        return []
    return questions

prompt_path_qatuples_gen = "qatuples_gen_no_filenames"
if USE_FILENAMES:
    prompt_path_qatuples_gen = "qatuples_gen_filenames"
    
qatuples_gen_regex = re.compile(
        r"Questions \(make 4\):\n(.+)", re.IGNORECASE | re.DOTALL
    )

class QuestionGenerationStep(PipelineStep): # like before, but with the new system. Override the read and save.
    def __init__(self):
        super().__init__(
            prompt_folder=PROMPTS_DIR,
            default_prompt_folder=DEFAULT_PROMPT_PATH,
            prompt_path=prompt_path_qatuples_gen,
            regex=qatuples_gen_regex,
            sampling_params={
                "max_tokens": 2000,
                "stop": [
                    "### Response",
                    "\n\n\n\n\n",
                    "</s>",
                    "# Input:",
                    "[INST]",
                    "### Instruction",
                    "[INST",
                    "<|eot_id|>",
                    "<|start_header_id|>",
                    "<|end_header_id|>",
                ],
                "temperature": 0.8,
                # top_k=-1,
                "top_p": 1,
                # min_p=0.5,
            },
            output_dir=OUTPUT_DIR,
            output_subdir="question_generation_generations",
            output_processor=extract_questions_from_response,
            use_stop=USE_STOP,
            intermediate_output_path="question_generation_generations",
            completion_mode=COMPLETION_MODE,
            save_path="raw_qatuples_saved",
            result_key="not_used",
        )
        
    def read_previous_output(self, idx, output_list):
        existing_files = glob.glob(
            os.path.join(self.save_path_dir, f"para_{idx}_*.json")
        )
        
        if len(existing_files) > 0:
            print(f"Skipping para_{idx} as files already exist; loading said files")
            for file_path in existing_files:
                with open(file_path, "r") as file:
                    qa_dict = json.load(file)
                output_list.append(qa_dict)
            return True
        return False
    
    def generate_data(self, processed_data, engine_wrapper):
        self.question_group_id = make_id()
        return super().generate_data(processed_data, engine_wrapper)
    
    def save(self, result=None, full_output=None, idx=None, output_list=None, input_data=None):

        id = make_id()
        write_output_to_file(full_output, self.intermediate_output_path_full, id)
        qdicts = [
            {
                "paragraph": input_data['paragraph'],
                "metadata": input_data['metadata'],
                "question": qatup[0],
                "answer": qatup[1],
                "question_group_id": self.question_group_id,
                "paragraph_idx": idx,
                "question_idx": qnum,
            } for qnum, qatup in enumerate(result)
        ]
        
        output_list.extend(qdicts)
        
        # Save the output to a file
        os.makedirs(self.save_path_dir, exist_ok=True)
        for qdict in qdicts:
            file_path = os.path.join(self.save_path_dir, f"para_{idx}_q_{qdict['question_idx']}.json")
            with open(file_path, "w") as file:
                json.dump(qdict, file, indent=4)

question_generation_step = QuestionGenerationStep() 


# Question generation
async def generate_qadicts_from_para(
    idx,
    para,
    engine_wrapper_large=None,
    generated_qa_dicts=None,
):
    # NOTE Set up qatuple generation step #
    
    await question_generation_step.run(
        idx=idx,
        input_data=para,
        engine_wrapper=engine_wrapper_large,
        output_list=generated_qa_dicts
    )


def filter_and_graph(dicts):
    # Count the occurrences of None and non-None for each source text
    source_counts = Counter()
    for dict in dicts:
        print(dict)
        if dict["paragraph"] is None:
            source_counts[dict["metadata"]] = source_counts.get(dict["metadata"], [0, 0])
            source_counts[dict["metadata"]][0] += 1
        else:
            source_counts[dict["metadata"]] = source_counts.get(dict["metadata"], [0, 0])
            source_counts[dict["metadata"]][1] += 1

    # Filter out tuples with None and return the new list
    filtered_list = [t for t in dicts if t["paragraph"] is not None]
    return filtered_list



### JUDGEMENT SECTION

if USE_FILENAMES:
    judgement_prompt_path = "judge_paragraph_filenames"
else:
    judgement_prompt_path = "judge_paragraph_no_filenames"

judgement_regex = re.compile(
        r"Reasoning and thought process \(reason intelligently\):(.+)",
        re.DOTALL | re.IGNORECASE,
    )

def judge_paragraph_processor(
    determination,
):  # TODO extract to separate file to avoid muddying the control flow code
    if "unsuitable" in determination.lower() or "table of contents" in determination.lower():
        return False  # control flow has been modified to use the information it has, based on the determination of the output processors
    elif "suitable" in determination.lower():
        return True

class JudgeParagraphStep(PipelineStep):
    def __init__(self): # instead of overriding init, just pass these when instantiating the class
        super().__init__(
            prompt_folder=PROMPTS_DIR,
            default_prompt_folder=DEFAULT_PROMPT_PATH,
            prompt_path=judgement_prompt_path,
            regex=judgement_regex,
            sampling_params={
                "max_tokens": 1450,
                # "min_p": 0.4,
                "stop": [
                    "### Response",
                    "\n\n\n\n\n\n\n\n\n\n\n\n\n",
                    "</s>",
                    "# Input:",
                    "[INST]",
                    "### Instruction",
                    "[INST",
                    "<|eot_id|>",
                    "<|start_header_id|>",
                    "<|end_header_id|>",
                ],
                "temperature": 0.2,
            },
            output_dir=OUTPUT_DIR,
            output_subdir="judge_paragraph_generations", # TODO rename to just judge_paragraph_all_outputs, same with q gen.
            output_processor=judge_paragraph_processor,
            use_stop=USE_STOP,
            intermediate_output_path="intermediate_generations",
            completion_mode=COMPLETION_MODE,
            save_path="saved_readable_generations",
            result_key="judged_worthy_for_questions",
        )
        
    def read_previous_output(self, idx, output_list):
        save_path_file = self.make_save_path_file(idx)
        
        if os.path.isfile(save_path_file):
            with open(save_path_file, "r") as f:
                data = json.load(f)
                if isinstance(data, str):
                    output_list.append(
                        {
                            "paragraph": None,
                            "metadata": data[7:]
                        }
                    )
                else:
                    output_list.append(
                        {
                            "paragraph": data["paragraph"], 
                            "metadata": data["metadata"]
                        }
                    )
            return True
        else:
            return False
    
    def save(self, result=None, full_output=None, idx=None, output_list=None, input_data=None):
        os.makedirs(self.full_output_path, exist_ok=True)
        save_path_file = self.make_save_path_file(idx)
        
        
        output_data = input_data
        print("DEBUG: RESULT")
        print(result)
        if not result:
            output_data = {
                "paragraph": None,
                "metadata": input_data["metadata"]
            }
            output_list.append(output_data)
            os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
            with open(save_path_file, "w") as f:
                metadata = input_data["metadata"]
                f.write(f"failed|{metadata}")
            print(f"DEBUG model decided that index {idx} was not suitable")
            print(f"Saved to {save_path_file}")
        else:
            output_data = {
                "paragraph": input_data["paragraph"],
                "metadata": input_data["metadata"]
            }
            output_list.append(output_data)
            os.makedirs(os.path.dirname(save_path_file), exist_ok=True)
            with open(save_path_file, "w") as f:
                json.dump(output_data, f)
            print(f"DEBUG model decided that index {idx} was suitable")
            print(f"Saved to {save_path_file}")
            
            
        write_output_to_file(full_output, self.intermediate_output_path_full, idx)
        
judge_paragraph_step = JudgeParagraphStep()

# EXEMPLAR
async def filter_all_questions(
    paragraphs_processed,
    judged_worthy_for_questions,
    engine_wrapper,
    take_subset=False,
    subset_size=None,
    use_filenames=False,
    rtwl=None,
    completion_mode=None,
    logging_level=None,
):
    if not take_subset:
        tasks = [
            # determine_worthy(idx, p, judged_worthy_for_questions, output_dir, engine_wrapper)
            judge_paragraph_step.run(idx, input_data=p, output_list=judged_worthy_for_questions, engine_wrapper=engine_wrapper)
            for idx, p in enumerate(paragraphs_processed)
        ]
    else:
        tasks = [
            # determine_worthy(idx, p, judged_worthy_for_questions, output_dir, engine_wrapper)
            judge_paragraph_step.run(idx, input_data=p, output_list=judged_worthy_for_questions, engine_wrapper=engine_wrapper)
            for idx, p in enumerate(paragraphs_processed[:subset_size])
        ]
    limited_tasks = [rtwl(task) for task in tasks]
    for future in tqdmasyncio.tqdm.as_completed(limited_tasks):
        await future


def fix_text(to_replace_arr, text):
    for tup in to_replace_arr:
        text = text.replace(tup[0], tup[1])
    return text


async def ensure_multiple_answers_are_same(
    info, conv, multi_turn_conv_generator, completion_mode=None, conversation_instructions="For this conversation, you are generating a chat between a general-purpose AI assistant and a human."
):  # why is this a whole separate function? Once upon a time, LLMs were used in validation here, too. But programmatic validation SEEMS to catch the common problems. This is here so that I can add it back in if I have to.
    """Loop to ensure that the answer is consistent in the conversation and in the tuple."""
    retries = 0
    c = conv
    while retries < 2:  # try twice, since multiturn is an expensive operation
        if process_multiturn_functions.call_all_processors(
            c[0], info[0]
        ):  # if programmatic validation passes
            return c

        retries += 1
        if retries >= 2:
            return None
        # If we're here, majority of relevance checks failed
        print("----------------\n\n\n\nRETRYING!!!!\n\n\n\n----------------")
        # Broken info is 1) rare and 2) handled by the retry limit. We don't want to waste compute on regenerating info as they take time.
        retry = await make_multiturn_conversation(
            info, multi_turn_conv_generator, completion_mode=completion_mode, conversation_instructions=conversation_instructions
        )
        if retry is not None:  # Note: retry CANNOT actually be None
            c = retry
        else:
            # If we failed to generate a retry, don't waste compute
            return None

    return None



async def make_multiturn_conversation(
    info, multi_turn_conv_generator, completion_mode=None, conversation_instructions="For this conversation, you are generating a chat between a general-purpose AI assistant and a human."
):

    conv, conv_output = await multi_turn_conv_generator.generate(
        arguments={
            "question_answer_list": format_qatuples(info[0]).strip(),
            "conversation_instructions": conversation_instructions
        }
    )
    write_output_to_file(
        conv_output,
        obj_conf["PATH"]["OUTPUT"] + "/multiturn_conversation_generations",
        info[4],
    )

    return (conv, info[1], info[2], info[3], info[0])

async def create_info(
    idx,
    group,
    multi_turn_convs_info,
    multi_turn_convs_info_dir,
):

    file_path = os.path.join(multi_turn_convs_info_dir, f"info_{idx}.json")

    # Skip if file already exists
    if not os.path.exists(file_path):
        info = (group, "will", "be", "replaced", make_id())

        with open(file_path, "w") as file:
            json.dump(info, file, indent=4)
    else:
        with open(file_path, "r") as file:
            info = json.load(file)

    multi_turn_convs_info.append(
        [info]
    )  # hacky-looking things because the legacy functionality was simplified.

def read_json_files_info(directory):
    # Create a list to hold the tuples
    tuple_list = []

    # Get all the .json files in the directory, sorted
    json_files = sorted([f for f in os.listdir(directory) if f.endswith(".json")])

    # Read each file and convert the contents
    for file in json_files:
        with open(os.path.join(directory, file), "r") as f:
            data = json.load(f)
            # Ensure the data is in the correct format before converting to tuple
            if (
                isinstance(data, list)
                and len(data) == 5
                and isinstance(data[0], list)
                and all(len(item) == 7 for item in data[0])
                and all(isinstance(i, str) for i in data[1:])
            ):
                tuple_list.append((data[0], data[1], data[2], data[3], data[4]))

    return tuple_list


async def create_conversation(
    idx,
    info,
    engine_wrapper,
    multi_turn_convs,
    multi_turn_convs_dir,
    completion_mode=None,
    logging_level=logging.INFO,
    conversation_instructions="For this conversation, you are generating a chat between a general-purpose AI assistant and a human."
):
    file_path = os.path.join(multi_turn_convs_dir, f"conv_{idx}.json")
    multi_turn_conversation_prompt_path = "multi_turn_assistant_conversation"

    conversation_regex = re.compile(
        f"Conversation that answers the provided question \(be sure that you do not change the questions or answers themselves; AI Assistant will answer the questions, not ask them; the questions and answers provided should be copied word for word, and surrounded by compelling conversation\):\n(.+)",
        re.IGNORECASE | re.DOTALL,
    )

    if completion_mode:
        multi_turn_conversation_prompt_path = (
            multi_turn_conversation_prompt_path + ".txt"
        )
    else:
        multi_turn_conversation_prompt_path = (
            multi_turn_conversation_prompt_path + ".yaml"
        )

    multi_turn_conv_generator = GenerationStep(
        prompt_path=multi_turn_conversation_prompt_path,
        regex=conversation_regex,
        sampling_params={
            "max_tokens": 2000,
            "stop": [
                "### Response",
                "\n\n\n\n\n",
                "</s>",
                "# Input:",
                "[INST]",
                "### Instruction",
                "### Information",
                "## Information",
                "## Instruction",
                "Name:",
                "<|eot_id|>",
                "<|start_header_id|>",
                "<|end_header_id|>",
            ],
            "temperature": 0.8,
            # "top_k": -1,
            "top_p": 1,
            # "min_p": 0.6,
        },
        completion_mode=completion_mode,
        retries=1,
        engine_wrapper=engine_wrapper,
        logging_level=logging_level,
        prompt_folder=obj_conf["PATH"]["PROMPTS"],
        default_prompt_folder=DEFAULT_PROMPT_PATH,
        use_stop=obj_conf["SYSTEM"]["STOP"],
    )

    # Skip if file already exists
    if not os.path.exists(file_path):
        try:
            conv = await make_multiturn_conversation(
                info, multi_turn_conv_generator, completion_mode=completion_mode, conversation_instructions=conversation_instructions
            )
            final_conv = await ensure_multiple_answers_are_same(
                info, conv, multi_turn_conv_generator, completion_mode=completion_mode, conversation_instructions=conversation_instructions
            )

            if final_conv is not None:
                final_conv = (
                    final_conv[0],
                    "AI Assistant",
                    "",
                    "N/A",
                    final_conv[4],
                )
                with open(file_path, "w") as file:
                    json.dump(final_conv, file, indent=4)

            multi_turn_convs.append(final_conv)
        except Exception as e:
            traceback.print_exc()
            print("Had an error, retrying...", e)
    else:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                multi_turn_convs.append(data)
            print(f"Skipped generating {file_path} as it already exists")
        except Exception as e:
            print(f"Error reading {file_path}:", e)
            print("Continuing...")


def convert_directory_to_list(directory_path):
    master_list = []
    simplified_list = []
    simplified_rag_list = []

    for filename in os.listdir(directory_path):  # for each file
        if filename.endswith(".json"):  # if it's a conversation file
            filepath = os.path.join(directory_path, filename)  # get the path
            with open(filepath, "r") as file:  # open it
                try:
                    data = json.load(file)  # load its data
                    if isinstance(data, list) and all(
                        isinstance(item, (list, str))
                        for item in data  # if it has the correct format
                    ):

                        data_dict = {
                            "conversation": data[0],
                            "qa_tuples": [
                                tup[:2] for tup in data[4]
                            ],  # only take first two items from each tuple
                            "rag_context": data[4][0][2],
                            "source_filename": data[4][0][3],
                        }
                        master_list.append(
                            data_dict
                        )  # append it as-is to the master-list

                        # Extract and process conversation
                        conversation, primary_char_desc = (
                            data[0],
                            data[1],
                        )  # first and second items are conv and char desc
                        dialogues = process_multiturn_functions.extract_conversation(
                            conversation
                        )

                        # Convert to simplified format
                        simplified_conversations = []
                        simplified_conversations_rag = []

                        # Load system prompts
                        system_prompt_norag = obj_conf["SYSTEM"][
                            "FINAL_ASSISTANT_PROMPT_NO_RAG"
                        ]
                        system_prompt_rag = obj_conf["SYSTEM"][
                            "FINAL_ASSISTANT_PROMPT_RAG"
                        ]
                        simplified_conversations.append(
                            {"from": "system", "value": system_prompt_norag}
                        )

                        simplified_conversations_rag.append(
                            {
                                "from": "system",
                                "value": system_prompt_rag.replace(
                                    "{data}", data_dict["rag_context"]
                                ),
                            }
                        )
                        for i, (charname, message) in enumerate(
                            dialogues
                        ):  # Skipping the first message
                            from_person = "human" if (i % 2) == 0 else "gpt"
                            simplified_conversations.append(
                                {"from": from_person, "value": f"{message}"}
                            )
                            simplified_conversations_rag.append(
                                {
                                    "from": from_person,
                                    "value": f"{message}",
                                }  # same as above, but for the RAG context
                            )

                        if simplified_conversations:  # If there are any conversations
                            simplified_list.append(
                                {"conversations": simplified_conversations}
                            )
                            simplified_rag_list.append(
                                {"conversations": simplified_conversations_rag}
                            )
                except Exception as e:
                    print(f"Error reading {filename}: {e}")

    
    
        # Write the master list to a new .jsonl file
    write_1 = obj_conf["PATH"]["OUTPUT"] + "/master_list.jsonl"
    with open(write_1, "w") as file:
        for item in master_list:
            file.write(json.dumps(item) + "\n")

    # Process and push simplified_list (no RAG)
    write_2 = obj_conf["PATH"]["OUTPUT"] + "/simplified_data_no_rag.jsonl"
    with open(write_2, "w") as file:
        for item in simplified_list:
            file.write(json.dumps(item) + "\n")

    if PUSH_TO_HUB:
        # Create a temporary JSON file with train split
        temp_file_no_rag = obj_conf["PATH"]["OUTPUT"] + "/temp_simplified_data_no_rag.json"
        with open(temp_file_no_rag, 'w') as temp_file:
            json.dump({"train": simplified_list}, temp_file)
        
        # Load the dataset from the temporary file
        dataset_no_rag = load_dataset("json", data_files=temp_file_no_rag, split="train")
        
        # Push to Hugging Face Hub
        dataset_no_rag.to_parquet(f"hf://datasets/{HUB_PATH}/data/train-no_rag.parquet")
        
        # Remove the temporary file
        os.remove(temp_file_no_rag)

    # Process and push simplified_rag_list (RAG)
    write_3 = obj_conf["PATH"]["OUTPUT"] + "/simplified_data_rag.jsonl"
    with open(write_3, "w") as file:
        for item in simplified_rag_list:
            file.write(json.dumps(item) + "\n")

    if PUSH_TO_HUB:
        # Create a temporary JSON file with train split
        temp_file_rag = obj_conf["PATH"]["OUTPUT"] + "/temp_simplified_data_rag.json"
        with open(temp_file_rag, 'w') as temp_file:
            json.dump({"train": simplified_rag_list}, temp_file)
        
        # Load the dataset from the temporary file
        dataset_rag = load_dataset("json", data_files=temp_file_rag, split="train")
        
        # Push to Hugging Face Hub
        dataset_rag.to_parquet(f"hf://datasets/{HUB_PATH}/data/train-rag.parquet")
        
        # Remove the temporary file
        os.remove(temp_file_rag)

    print(
        f"Conversion complete. Master list written to {write_1}. Simplified data written to {write_2} (no RAG) and {write_3} (RAG)."
    )
    if PUSH_TO_HUB:
        print("Data successfully pushed to Hugging Face Hub.")


def convert_directory_and_process_conversations(directory_path):
    master_list = []

    for filename in os.listdir(directory_path):
        if filename.endswith(".json"):
            filepath = os.path.join(directory_path, filename)
            with open(filepath, "r") as file:
                try:
                    data = json.load(file)

                    if isinstance(data, list) and all(
                        isinstance(item, (list, str)) for item in data
                    ):
                        # Extract and process the conversation part
                        conversations = (
                            process_multiturn_functions.extract_conversation(data[0])
                        )
                        # Convert tuples back to the formatted string as required
                        data[0] = [
                            f"{charname}: {message}"
                            for charname, message in conversations
                        ]
                        master_list.append(data)
                    else:
                        print(f"File {filename} is not in the expected format.")
                except:
                    print(f"Error reading {filename}")

    # Write the master list to a new file
    with open(obj_conf["PATH"]["OUTPUT"] + "/processed_master_list.json", "w") as file:
        json.dump(master_list, file)

    print(
        "Conversion complete. The processed master list is written to 'processed_master_list.json'."
    )
