# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""This module is edited version of raw_rnn as define in rnn.py (tensorflow version 1.9.0rc2)
This edited raw_rnn is used for beam search
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.python.eager import context
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_util
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import rnn_cell_impl
from tensorflow.python.ops import tensor_array_ops
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.util import nest
from tensorflow.python.ops import logging_ops
from tensorflow.python.ops import functional_ops
from numpy import inf
from numpy import arange

# pylint: disable=protected-access
_concat = rnn_cell_impl._concat

eos_vocab_id = 0
# pylint: enable=protected-access


def _maybe_tensor_shape_from_tensor(shape):
    if isinstance(shape, ops.Tensor):
        return tensor_shape.as_shape(tensor_util.constant_value(shape))
    else:
        return shape


def raw_rnn_for_beam_search(cell, loop_fn,
                            parallel_iterations=None, swap_memory=False, scope=None):
    rnn_cell_impl.assert_like_rnncell("cell", cell)

    if not callable(loop_fn):
        raise TypeError("loop_fn must be a callable")

    parallel_iterations = parallel_iterations or 32

    # Create a new scope in which the caching device is either
    # determined by the parent scope, or is set to place the cached
    # Variable using the same placement as for the rest of the RNN.
    with vs.variable_scope(scope or "rnn") as varscope:
        if not context.executing_eagerly():
            if varscope.caching_device is None:
                varscope.set_caching_device(lambda op: op.device)

        time = constant_op.constant(0, dtype=dtypes.int32)
        (elements_finished, next_input, initial_state, emit_predicted_ids_structure,
         init_log_probs, init_beam_finished, _) = loop_fn(
            time, None, None, None, None)  # time, cell_output, cell_state, log_probs, beam_finished
        flat_input = nest.flatten(next_input)

        # Need a surrogate log_probs, beam_finished for the while_loop if none is available.
        log_probs = (init_log_probs if init_log_probs is not None
                      else constant_op.constant(0, dtype=dtypes.float32))
        beam_finished = (init_beam_finished if init_beam_finished is not None
                     else constant_op.constant(False, dtype=dtypes.bool))
        penalty_lengths = array_ops.zeros_like(log_probs, dtype=dtypes.float32)
        final_log_probs = array_ops.ones_like(log_probs, dtype=dtypes.float32)

        input_shape = [input_.get_shape() for input_ in flat_input]
        static_batch_size = input_shape[0][0]

        for input_shape_i in input_shape:
            # Static verification that batch sizes all match
            static_batch_size.merge_with(input_shape_i[0])

        batch_size = static_batch_size.value
        const_batch_size = batch_size
        if batch_size is None:
            batch_size = array_ops.shape(flat_input[0])[0]

        # nest.assert_same_structure(initial_state, cell.state_size)
        #  Note: remove above line because state will be tuple with number of elements based on beam width
        state = initial_state
        flat_state = nest.flatten(state)
        flat_state = [ops.convert_to_tensor(s) for s in flat_state]
        state = nest.pack_sequence_as(structure=state,
                                      flat_sequence=flat_state)

        if emit_predicted_ids_structure is not None:
            flat_emit_structure = nest.flatten(emit_predicted_ids_structure)
            flat_emit_size = [emit.shape if emit.shape.is_fully_defined() else
                              array_ops.shape(emit) for emit in flat_emit_structure]
            flat_emit_dtypes = [emit.dtype for emit in flat_emit_structure]
        else:
            emit_predicted_ids_structure = cell.output_size
            flat_emit_size = nest.flatten(emit_predicted_ids_structure)
            flat_emit_dtypes = [flat_state[0].dtype] * len(flat_emit_size)

        flat_emit_ta = [
            tensor_array_ops.TensorArray(
                dtype=dtype_i,
                dynamic_size=True,
                element_shape=(tensor_shape.TensorShape([const_batch_size])
                    .concatenate(
                    _maybe_tensor_shape_from_tensor(size_i))),
                size=0,
                clear_after_read=False,
                name="rnn_output_%d" % i)
            for i, (dtype_i, size_i)
            in enumerate(zip(flat_emit_dtypes, flat_emit_size))]
        predicted_ids_ta = nest.pack_sequence_as(structure=emit_predicted_ids_structure,
                                        flat_sequence=flat_emit_ta)
        flat_zero_emit = [
            array_ops.zeros(_concat(batch_size, size_i), dtype_i)
            for size_i, dtype_i in zip(flat_emit_size, flat_emit_dtypes)]
        zero_emit = nest.pack_sequence_as(structure=emit_predicted_ids_structure,
                                          flat_sequence=flat_zero_emit)

        # parent_ids_in_beam_ta = tensor_array_ops.TensorArray(dtypes.int32, size=0,
        #     dynamic_size=True, clear_after_read=False).write(0, initial_parent_ids_value)
        parent_ids_in_beam_ta = tensor_array_ops.TensorArray(dtypes.int32, size=0,
                                                            dynamic_size=True, clear_after_read=False)
        beam_width = array_ops.shape(log_probs)[-1]
        index_for_finished_beam = array_ops.stack([math_ops.range(beam_width)] * batch_size)

        def condition(unused_time, elements_finished, *_):
            return math_ops.logical_not(math_ops.reduce_all(elements_finished))

        def body(time, elements_finished, current_input,
                 _predicted_ids_ta, state, log_probs, parent_index_ta, beam_finished, penalty_lengths, _final_log_probs):
            """Internal while loop body for raw_rnn.

            Args:
              time: time scalar.
              elements_finished: batch-size vector.
              current_input: possibly nested tuple of input tensors.
              _predicted_ids_ta: possibly nested tuple of output TensorArrays.
              state: possibly nested tuple of state tensors.
              log_probs: possibly nested tuple of loop state tensors.
              parent_index_ta: index of previous word in beam (use in finding path)
              _final_log_probs: log_probs table indicate the log_prob values of beams according to penalty_length
            Returns:
              Tuple having the same size as Args but with updated values.
            """
            # ===========new code==================
            tuple_arr = [cell(_input, _state) for _input, _state in zip(current_input, state)]
            # ====================================
            # (next_output, cell_state) = cell(current_input, state)
            #  Note: above line is removed because beam search

            # =============new code================
            next_output = tuple(_output for _output, _ in tuple_arr)
            cell_state = tuple(_state for _, _state in tuple_arr)
            # =====================================
            nest.assert_same_structure(state, cell_state)
            # nest.assert_same_structure(cell.output_size, next_output)
            #  Note: above line is removed because beam search

            next_time = time + 1
            (next_finished, next_input, next_state, predicted_ids,
             new_log_probs, new_beam_finished, parent_indexs) = loop_fn(
                time, next_output, cell_state, log_probs, beam_finished)

            nest.assert_same_structure(state, next_state)
            nest.assert_same_structure(current_input, next_input)
            nest.assert_same_structure(_predicted_ids_ta, predicted_ids)
            # predicted_ids = logging_ops.Print(predicted_ids, [predicted_ids])
            # <eos> if finished at previous step
            predicted_ids = array_ops.where(
                beam_finished, array_ops.fill(array_ops.shape(predicted_ids), eos_vocab_id), predicted_ids)
            # predicted_ids = logging_ops.Print(predicted_ids, [predicted_ids[1]], message='ids[1] after where clause=')
            # should predict <eos> if finished
            # first update final_log_probs
            final_log_probs_not_updated = math_ops.equal(_final_log_probs, 1.)  # initial value is 1.0
            new_final_log_probs = array_ops.where(math_ops.logical_and(new_beam_finished, final_log_probs_not_updated),
                                                  new_log_probs, _final_log_probs)  # stay unchange if updated
            # new_final_log_probs = logging_ops.Print(new_final_log_probs, [new_final_log_probs[10]], message='new_final_log_probs=')
            # new_log_probs = logging_ops.Print(new_log_probs, [new_log_probs[10]], message='new_log_probs=')
            # then change log_probs of finished beams to -inf to never be chosen by top_k
            new_log_probs = array_ops.where(new_beam_finished,
                                array_ops.fill(array_ops.shape(log_probs), -inf), new_log_probs)
            # new_log_probs = logging_ops.Print(new_log_probs, [new_log_probs])
            penalty_lengths = array_ops.where(
                new_beam_finished, penalty_lengths, penalty_lengths + 1)  # +1 if NOT finished


            def _copy_some_through(current, candidate):
                """Copy some tensors through via array_ops.where."""

                def copy_fn(cur_i, cand_i):
                    # TensorArray and scalar get passed through.
                    if isinstance(cur_i, tensor_array_ops.TensorArray):
                        return cand_i
                    if cur_i.shape.ndims == 0:
                        return cand_i
                    # Otherwise propagate the old or the new value.
                    with ops.colocate_with(cand_i):
                        return array_ops.where(elements_finished, cur_i, cand_i)

                return nest.map_structure(copy_fn, current, candidate)

            predicted_ids = _copy_some_through(zero_emit, predicted_ids)
            # predicted_ids = logging_ops.Print(predicted_ids, [predicted_ids[1]], message='ids[1] after copy_some_through')
            next_state = _copy_some_through(state, next_state)

            _predicted_ids_ta = nest.map_structure(
                lambda ta, emit: ta.write(time, emit), _predicted_ids_ta, predicted_ids)

            # parent_indexs = control_flow_ops.cond(math_ops.equal(time, 0),
            #                 lambda: parent_indexs,  # pass it if time=0 (all filled with -1)
            #                 lambda: array_ops.where(new_beam_finished,
            #                         parent_index_ta.read(time - 1), parent_indexs))  # prev_ids if beam is finish)

            parent_indexs = control_flow_ops.cond(math_ops.equal(time, 0),
                              lambda: parent_indexs,  # pass it if time=0 (all filled with -1)
                              lambda: array_ops.where(beam_finished,
                                                      index_for_finished_beam,  # true index 0,1,2,...,beam
                                                      parent_indexs))

            # parent_indexs = logging_ops.Print(parent_indexs, [parent_indexs[1]], message='parent[1]=')
            parent_index_ta = parent_index_ta.write(time, parent_indexs)

            elements_finished = math_ops.logical_or(elements_finished, next_finished)

            return (next_time, elements_finished, next_input,
                    _predicted_ids_ta, next_state, new_log_probs,
                    parent_index_ta, new_beam_finished, penalty_lengths, new_final_log_probs)

        returned = control_flow_ops.while_loop(
            condition, body, loop_vars=[
                time, elements_finished, next_input,
                predicted_ids_ta, state, log_probs,
                parent_ids_in_beam_ta, beam_finished, penalty_lengths, final_log_probs],
            parallel_iterations=parallel_iterations,
            swap_memory=swap_memory)

        (_, _, _,
         predicted_ids_ta, _, _,
         parent_ids_ta, _, penalties, final_log_probs) = returned

        # for some elements of final_log_probs still are 1.0 (stop by reaching sentence length)
        # we need to turn it into -inf so it won't be chosen after normalize
        final_log_probs_still_not_updated = math_ops.equal(final_log_probs, 1.)
        final_log_probs = array_ops.where(final_log_probs_still_not_updated,
                                          array_ops.fill(array_ops.shape(final_log_probs), -inf),
                                          final_log_probs)

        return predicted_ids_ta, parent_ids_ta, penalties, final_log_probs


def extract_from_tree(predicted_ids, parent_ids, batch_size, beam_width):
    """
    Start from leaf nodes, we trace back to the root node
    :param predicted_ids: TensorArray of shape [time, batch, beam] that contains predicted ids after each time step
    :param parent_ids: TensorArray of shape [time, batch, beam] that contains the previous node at each time step
                        the very first root is -1 so we just back track to time=1
    Note:
    _ Let consider node j, predicted_ids[j] will be ids of previous node, while parent_ids[j]
        will be the previous node of previous node
    _ TensorArray with clear_after_read=False
    :return: TensorArray of word ids of predicted sentences. Shape [time, batch, beam]
    """
    seq_len = predicted_ids.size()
    # parent_ids will be sorted to match beam orders (0, 1, 2, ... , beam_width - 1)
    init_beam_sorted_index = array_ops.stack([arange(beam_width)] * batch_size)
    sentence_ids_ta = tensor_array_ops.TensorArray(dtypes.int32, size=seq_len, clear_after_read=False)
    def cond(i, *_):
        return math_ops.greater_equal(i, 0)
    def body(i, arr, sorted_parent_index):
        current_predited_ids = predicted_ids.read(i)  # [batch, beam]
        back_track_ids = get_word_ids(sorted_parent_index, current_predited_ids, batch_size)
        back_track_ids = array_ops.stack(back_track_ids)
        arr = arr.write(i, back_track_ids)

        current_parent_ids = parent_ids.read(i)  # [batch, beam]
        next_parent_index = get_word_ids(sorted_parent_index, current_parent_ids, batch_size)
        next_parent_index = array_ops.stack(next_parent_index)
        return i-1, arr, next_parent_index

    _, sentence_ids_ta, _ = control_flow_ops.while_loop(cond, body, [seq_len-1, sentence_ids_ta, init_beam_sorted_index])
    return sentence_ids_ta


def get_word_ids(ids, ilist, batch_size):
    """
    get values of ilist according to ids
    :param ids: list of indices, shape [batch, num_indices]
    :param ilist: list of list, shape [batch, list_size]
    :return: list of map objects
    """
    word_ids = []
    for which_sentence in range(batch_size):
        current_ids = ids[which_sentence]
        current_list = ilist[which_sentence]
        word_ids.append(functional_ops.map_fn(
            lambda id: current_list[id],
            elems=current_ids,
            dtype=dtypes.int32
        ))
    return word_ids