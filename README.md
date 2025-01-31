Machine Translation with Tensorflow

VERSION 1
1) attention_model_v1

_ Encoder has 2 layers: 1st layer is bidiretional lstm and 2nd layer is 2 stacked lstms

_ Decoder has 1 lstm with attention

_ Encoder pass both cell state and last hidden state to Decoder as initial state at first decode step (time=0)
2) infer_attention_model_v1

a) Greedy Search
* with decay learning rate after every 4 epochs

_ tst2012: bleu= 20.476983485168752, max_order=4, smooth=False

_ tst2013: bleu= 23.507148735156456, max_order=4, smooth=False
* learning rate fixed to 1.0

_ tst2012: bleu= 18.770609325156386, max_order=4, smooth=False

_ tst2013: bleu= 20.645434410841183, max_order=4, smooth=False

b) Beam Search:

_ tst2012: bleu=19.337257289027225, max_order=4, smooth=False, beam_width=3

_ tst2013: bleu=22.512314783841234, max_order=4, smooth=False, beam_width=3

_ tst2012: bleu=18.78559050178904, max_order=4, smooth=False, beam_width=10

_ tst2013: bleu=21.232249654450417, max_order=4, smooth=False, beam_width=10

VERSION 2

Same graph as version 1

Difference: just last hidden state of Encoder pass to Decoder at first step, cell state of Decoder will be zeros

Greedy Search
* with decay learning rate after every 4 epochs

_ tst2012: bleu=20.088968527140327, max_order=4, smooth=False

_ tst2013: bleu=22.970538348802798, max_order=4, smooth=False

Beam Search:

_ tst2012: bleu=, max_order=4, smooth=False, beam=3

_ tst2013: bleu=21.15102903522015, max_order=4, smooth=False, beam=10
