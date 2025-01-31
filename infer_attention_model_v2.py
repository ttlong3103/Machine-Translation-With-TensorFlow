import tensorflow as tf
from utils import embedding
import os
import numpy as np
import bleu
from beam_search import raw_rnn_for_beam_search
from beam_search import extract_from_tree
from beam_search import get_word_ids

eos_vocab_id = 0
sos_vocab_id = 2
unk_vocab_id = 1


def create_dataset(sentences_as_ids):
    def generator():
        for sentence in sentences_as_ids:
            yield sentence

    dataset = tf.data.Dataset.from_generator(generator, output_types=tf.int32)
    return dataset


def test_model(model_path, src_file_name, tgt_file_name, beam_width=1):
    infer_graph = tf.Graph()
    with infer_graph.as_default():
        data_path = 'data/'  # path of data folder
        embeddingHandler = embedding.Embedding()

        ############### load embedding for source language ###############
        src_input_path = data_path + src_file_name  # path to training file used for encoder
        src_embedding_output_path = data_path + 'embedding.vi'  # path to file word embedding
        src_vocab_path = data_path + 'vocab.vi'  # path to file vocabulary

        vocab_src, dic_src = embeddingHandler.load_vocab(src_vocab_path)
        sentences_src = embeddingHandler.load_sentences(src_input_path)
        if not os.path.exists(src_embedding_output_path):
            word2vec_src = embeddingHandler.create_embedding(sentences_src, vocab_src, src_embedding_output_path)
        else:
            word2vec_src = embeddingHandler.load_embedding(src_embedding_output_path)
        embedding_src = embeddingHandler.parse_embedding_to_list_from_vocab(word2vec_src, vocab_src)
        embedding_src = tf.constant(embedding_src)

        ################ load embedding for target language ####################
        tgt_input_path = data_path + tgt_file_name
        tgt_embedding_output_path = data_path + 'embedding.en'
        tgt_vocab_path = data_path + 'vocab.en'

        vocab_tgt, dic_tgt = embeddingHandler.load_vocab(tgt_vocab_path)
        sentences_tgt = embeddingHandler.load_sentences(tgt_input_path)
        if not os.path.exists(tgt_embedding_output_path):
            word2vec_tgt = embeddingHandler.create_embedding(sentences_tgt, vocab_tgt, tgt_embedding_output_path)
        else:
            word2vec_tgt = embeddingHandler.load_embedding(tgt_embedding_output_path)
        embedding_tgt = embeddingHandler.parse_embedding_to_list_from_vocab(word2vec_tgt, vocab_tgt)
        embedding_tgt = tf.constant(embedding_tgt)

        if word2vec_src.vector_size != word2vec_tgt.vector_size:
            print('Word2Vec dimension not equal')
            exit(1)
        if len(sentences_src) != len(sentences_tgt):
            print('Source and Target data not match number of lines')
            exit(1)
        word2vec_dim = word2vec_src.vector_size  # dimension of a vector of word

        ################## create dataset ######################
        batch_size = 64

        # create training set for encoder (source)
        sentences_src_as_ids = embeddingHandler.convert_sentences_to_ids(dic_src, sentences_src)
        for sentence in sentences_src_as_ids:  # add <eos>
            sentence.append(eos_vocab_id)
        test_set_src = create_dataset(sentences_src_as_ids)
        test_set_src_len = create_dataset([[len(s)] for s in sentences_src_as_ids])

        # create training set for decoder (target)
        sentences_tgt_as_ids = embeddingHandler.convert_sentences_to_ids(dic_tgt, sentences_tgt)
        # for sentence_as_ids in sentences_tgt_as_ids:  # add </s> id to the end of each sentence of target language
        #     sentence_as_ids.append(eos_vocab_id)
        test_set_tgt = create_dataset(sentences_tgt_as_ids)
        test_set_tgt_len = create_dataset([[len(sentence) + 1] for sentence in sentences_tgt_as_ids])
        # Note: [len(sentence)+1] for later <sos>/<eos>
        test_set_tgt_padding = create_dataset(
            [np.ones(len(sentence) + 1, np.float32) for sentence in sentences_tgt_as_ids])

        # create dataset contains both previous training sets
        train_dataset = tf.data.Dataset.zip(
            (test_set_src, test_set_tgt, test_set_src_len, test_set_tgt_len, test_set_tgt_padding))
        train_dataset = train_dataset.apply(
            tf.contrib.data.padded_batch_and_drop_remainder(batch_size, ([None], [None], [1], [1], [None])))
        train_iter = train_dataset.make_initializable_iterator()
        x_batch, y_batch, len_xs, len_ys, padding_mask = train_iter.get_next()
        # Note: len_xs and len_ys have shape [batch_size, 1]

        #################### build graph ##########################
        hidden_size = word2vec_dim  # number of hidden unit
        encode_seq_lens = tf.reshape(len_xs, shape=[batch_size])
        # ---------encoder first layer
        enc_1st_outputs, enc_1st_states = tf.nn.bidirectional_dynamic_rnn(
            cell_fw=tf.nn.rnn_cell.BasicLSTMCell(hidden_size),
            cell_bw=tf.nn.rnn_cell.BasicLSTMCell(hidden_size),
            inputs=tf.nn.embedding_lookup(embedding_src, x_batch),
            sequence_length=encode_seq_lens,
            swap_memory=True,
            time_major=False,
            dtype=tf.float32
        )  # [batch, time, hid]
        fw_enc_1st_hid_states, bw_enc_1st_hid_states = enc_1st_outputs
        # fw_enc_1st_last_hid, bw_enc_1st_last_hid = enc_1st_states

        # ----------encoder second layer
        num_layers = 2
        stacked_lstm = tf.nn.rnn_cell.MultiRNNCell(
            [tf.nn.rnn_cell.BasicLSTMCell(hidden_size * 2)] * num_layers
        )
        enc_2nd_outputs, enc_2nd_states = tf.nn.dynamic_rnn(
            cell=stacked_lstm,
            inputs=tf.concat([fw_enc_1st_hid_states, bw_enc_1st_hid_states], axis=-1),
            sequence_length=encode_seq_lens,
            dtype=tf.float32,
            swap_memory=True,
            time_major=False
        )

        # ----------decoder
        encode_output_size = hidden_size * 2
        # decode_seq_lens = tf.reshape(len_ys, shape=[batch_size])
        decode_seq_lens = encode_seq_lens * 2  # maximum iterations
        attention_output_size = 256
        attention_mechanism = tf.contrib.seq2seq.LuongAttention(
            num_units=encode_output_size,
            memory=enc_2nd_outputs,  # require [batch, time, ...]
            memory_sequence_length=encode_seq_lens,
            dtype=tf.float32
        )
        attention_cell = tf.nn.rnn_cell.BasicLSTMCell(num_units=encode_output_size)
        attention_cell = tf.contrib.seq2seq.AttentionWrapper(
            attention_cell, attention_mechanism,
            attention_layer_size=attention_output_size
        )
        state_to_clone = attention_cell.zero_state(dtype=tf.float32, batch_size=batch_size)
        decoder_initial_state = tf.contrib.seq2seq.AttentionWrapperState(
            cell_state=tf.nn.rnn_cell.LSTMStateTuple(
                c=tf.zeros_like(enc_2nd_states[-1].c, dtype=tf.float32),
                h=enc_2nd_states[-1].h
            ),
            attention=state_to_clone.attention,
            time=state_to_clone.time,
            alignments=state_to_clone.alignments,
            alignment_history=state_to_clone.alignment_history,
            attention_state=state_to_clone.attention_state
        )

        # projection
        tgt_vocab_size = len(vocab_tgt)
        weight_score = tf.Variable(
            tf.random_uniform(shape=[attention_output_size, tgt_vocab_size], minval=-0.1, maxval=0.1)
        )
        bias_score = tf.Variable(
            tf.zeros([batch_size, tgt_vocab_size])
        )

        # beam search
        def loop_fn(time, cell_output, cell_state, log_probs, beam_finished):
            elements_finished = time >= decode_seq_lens  # finish by sentence length
            if cell_output is None:  # initialize step
                next_cell_state = tuple(decoder_initial_state for _ in range(beam_width))
                next_input = tuple(
                    tf.nn.embedding_lookup(embedding_tgt, [sos_vocab_id] * batch_size) for _ in range(beam_width))
                predicted_ids = tf.convert_to_tensor([0] * beam_width)  # https://github.com/hanxiao/hanxiao.github.io/issues/8
                new_log_probs = tf.zeros([batch_size, beam_width])
                new_beam_finished = tf.fill([batch_size, beam_width], value=False)
                parent_indexs = None
            else:
                def not_time_0():
                    next_cell_state = cell_state
                    # find predicted_ids
                    values_list = []
                    indices_list = []
                    for i in range(beam_width):
                        score = tf.add(
                            tf.matmul(cell_output[i], weight_score), bias_score
                        )
                        softmax = tf.nn.softmax(score)
                        log_prob = tf.log(softmax)
                        values, indices = tf.nn.top_k(log_prob, beam_width,
                                                      sorted=True)  # [batch, beam], [batch, beam]
                        # Note: indices is ids of words as well
                        values = tf.add(values, tf.expand_dims(log_probs[:, i], -1))  # sum with previous log_prob
                        values_list.append(values)
                        indices_list.append(indices)
                    concat_vlist = tf.concat(tf.unstack(values_list, axis=0),
                                             axis=-1)  # [batch_size, beam_width*beam_width]
                    concat_ilist = tf.concat(tf.unstack(indices_list, axis=0), axis=-1)
                    top_values, index_in_vlist = tf.nn.top_k(concat_vlist, beam_width,
                                                             sorted=True)  # [batch_size, beam_width]
                    # Note: in tf.nn.top_k, if sorted=False then it's values will be SORTED ASCENDING

                    predicted_ids = get_word_ids(index_in_vlist, concat_ilist, batch_size)
                    predicted_ids = tf.stack(predicted_ids)  # [batch_size, beam_width]

                    # new_beam_finished = tf.logical_or(tf.equal(predicted_ids, eos_vocab_id), beam_finished)

                    # find parent_ids that match word_ids_to_add
                    parent_indexs = index_in_vlist // beam_width
                    # find new_log_probs
                    new_log_probs = top_values

                    # shift top-k according to beam_finished
                    # which means we will shift predicted_ids, new_log_probs, parent_indexs
                    def shift(tensor_1D, num_shift, vacancy_value):
                        """
                        shift from left to right
                        """
                        shift_value = tensor_1D[:beam_width - num_shift]
                        fill_vacancy = tf.fill([num_shift], vacancy_value)
                        return tf.concat([fill_vacancy, shift_value], axis=0)

                    ids_arr = []
                    probs_arr = []
                    parents_arr = []
                    num_shifts = tf.map_fn(lambda beam: tf.reduce_sum(tf.cast(beam, tf.int32)),
                                           beam_finished, dtype=tf.int32)
                    # Note: we don't shift using new_beam_finished to avoid newly finish
                    # which will update -inf to final_log_probs
                    for i in range(batch_size):
                        num_shift = num_shifts[i]
                        ids_arr.append(shift(predicted_ids[i], num_shift, eos_vocab_id))
                        probs_arr.append(shift(new_log_probs[i], num_shift, -np.inf))
                        parents_arr.append(shift(parent_indexs[i], num_shift, -1))
                    valid_shape = tf.shape(beam_finished)
                    predicted_ids = tf.stack(ids_arr)
                    predicted_ids = tf.reshape(predicted_ids, valid_shape)
                    new_log_probs = tf.stack(probs_arr)
                    new_log_probs = tf.reshape(new_log_probs, valid_shape)
                    parent_indexs = tf.stack(parents_arr)
                    parent_indexs = tf.reshape(parent_indexs, valid_shape)

                    new_beam_finished = tf.logical_or(tf.equal(predicted_ids, eos_vocab_id), beam_finished)

                    # define next_input
                    finished = tf.reduce_all(elements_finished)
                    next_input = tuple(
                        tf.cond(
                            finished,
                            lambda: tf.nn.embedding_lookup(embedding_tgt, [eos_vocab_id] * batch_size),
                            lambda: tf.nn.embedding_lookup(embedding_tgt, predicted_ids[:, i])
                        ) for i in range(beam_width)
                    )

                    return elements_finished, next_input, next_cell_state, predicted_ids, new_log_probs, new_beam_finished, parent_indexs

                def time_0():
                    next_cell_state = cell_state
                    # find next_input
                    score = tf.add(
                        tf.matmul(cell_output[0], weight_score), bias_score
                    )
                    softmax = tf.nn.softmax(score)
                    log_prob = tf.log(softmax)
                    top_values, predicted_ids = tf.nn.top_k(log_prob, beam_width,
                                                            sorted=True)  # [batch_size, beam_width]

                    new_beam_finished = beam_finished

                    parent_indexs = tf.fill([batch_size, beam_width], value=-1)

                    new_log_probs = top_values

                    finished = tf.reduce_all(elements_finished)
                    next_input = tuple(
                        tf.cond(
                            finished,
                            lambda: tf.nn.embedding_lookup(embedding_tgt, [eos_vocab_id] * batch_size),
                            lambda: tf.nn.embedding_lookup(embedding_tgt, predicted_ids[:, i])
                        ) for i in range(beam_width)
                    )

                    return elements_finished, next_input, next_cell_state, predicted_ids, new_log_probs, new_beam_finished, parent_indexs

                # Important note: we won't feed <sos> at step 0 because it will lead to all same results on all beams
                # instead, we feed top-k predictions generated from feeding <sos> as input
                # other returns will be pass without change
                elements_finished, next_input, next_cell_state, predicted_ids, new_log_probs, new_beam_finished, parent_indexs = tf.cond(
                    tf.equal(time, 0), time_0, not_time_0)

            return elements_finished, next_input, next_cell_state, predicted_ids, new_log_probs, new_beam_finished, parent_indexs

        predicted_ids_ta, parent_ids_ta, penalty_lengths, final_log_probs = raw_rnn_for_beam_search(attention_cell,
                                                                                                    loop_fn)
        translation_ta = extract_from_tree(predicted_ids_ta, parent_ids_ta, batch_size, beam_width)
        outputs = translation_ta.stack()  # [time, batch, beam]
        # choose best translation with maximum sum log probability
        normalize_log_probs = final_log_probs / penalty_lengths
        chosen_translations = tf.argmax(normalize_log_probs, axis=-1, output_type=tf.int32)  # [batch]
        transpose_outputs = tf.transpose(outputs, perm=[1, 2, 0])  # transpose to [batch, beam, time]
        final_output = get_word_ids(tf.expand_dims(chosen_translations, -1), transpose_outputs, batch_size)
        final_output = tf.stack(final_output)  # [batch, 1, time]
        final_output = tf.reshape(final_output, [batch_size, -1])  # [batch, time]

        #################### train ########################
        saver = tf.train.Saver()
        with tf.Session() as sess:
            saver.restore(sess, model_path)
            sess.run(train_iter.initializer)
            references = []
            # Note: references has shape 3-d to pass into compute_bleu function
            # first dimension is batch size, second dimension is number of references for 1 translation
            # third dimension is length of each sentence (maybe differ from each other)
            translation = []
            while True:
                # for i in range(10):
                #     print(i)
                try:
                    predictions, labels = sess.run([final_output, y_batch])
                    # perform trimming <eos> to not to get additional bleu score by overlap padding
                    predictions = [np.trim_zeros(predict, 'b') for predict in predictions]
                    labels = [np.trim_zeros(lb, 'b') for lb in labels]
                    # # convert ids to words
                    # predictions = [embeddingHandler.ids_to_words(predict, vocab_tgt) for predict in predictions]
                    # labels = [embeddingHandler.ids_to_words(lb, vocab_tgt) for lb in labels]
                    references.extend(labels)
                    translation.extend(predictions)
                except tf.errors.OutOfRangeError:
                    break

            # compute bleu score
            reshaped_references = [[ref] for ref in references]
            bleu_score, *_ = bleu.compute_bleu(reshaped_references, translation, max_order=4, smooth=False)
            return bleu_score


bleu = test_model(model_path='checkpoint_v2/model-11', src_file_name='tst2012.vi', tgt_file_name='tst2012.en', beam_width=3)
print(bleu*100)