#!/usr/bin/env python
from __future__ import division
from __future__ import print_function
import os
import sys
import gzip
import logging
import argparse
import resource
import subprocess
import networkx

VERBOSE = (logging.DEBUG+logging.INFO)//2
ARG_DEFAULTS = {'sam':sys.stdin, 'qual':20, 'pos':2, 'dist':1, 'choose_by':'reads', 'output':True,
                'visualize':0, 'viz_format':'png', 'log':sys.stderr, 'volume':logging.WARNING}
USAGE = "%(prog)s [options]"
DESCRIPTION = """Correct barcodes using an alignment of all barcodes to themselves. Reads the
alignment in SAM format and corrects the barcodes in an input "families" file (the output of
make-barcodes.awk). It will print the "families" file to stdout with barcodes (and orders)
corrected."""


def main(argv):

  parser = argparse.ArgumentParser(description=DESCRIPTION)
  parser.set_defaults(**ARG_DEFAULTS)

  parser.add_argument('families', type=open_as_text_or_gzip,
    help='The sorted output of make-barcodes.awk. The important part is that it\'s a tab-delimited '
         'file with at least 2 columns: the barcode sequence and order, and it must be sorted in '
         'the same order as the "reads" in the SAM file.')
  parser.add_argument('reads', type=open_as_text_or_gzip,
    help='The fasta/q file given to the aligner. Used to get barcode sequences from read names.')
  parser.add_argument('sam', type=argparse.FileType('r'), nargs='?',
    help='Barcode alignment, in SAM format. Omit to read from stdin. The read names must be '
         'integers, representing the (1-based) order they appear in the families file.')
  parser.add_argument('-P', '--prepend', action='store_true',
    help='Prepend the corrected barcodes and orders to the original columns.')
  parser.add_argument('-d', '--dist', type=int,
    help='NM edit distance threshold. Default: %(default)s')
  parser.add_argument('-m', '--mapq', type=int,
    help='MAPQ threshold. Default: %(default)s')
  parser.add_argument('-p', '--pos', type=int,
    help='POS tolerance. Alignments will be ignored if abs(POS - 1) is greater than this value. '
         'Set to greater than the barcode length for no threshold. Default: %(default)s')
  parser.add_argument('-t', '--tag-len', type=int,
    help='Length of each half of the barcode. If not given, it will be determined from the first '
         'barcode in the families file.')
  parser.add_argument('-c', '--choose-by', choices=('reads', 'connectivity'))
  parser.add_argument('--limit', type=int,
    help='Limit the number of lines that will be read from each input file, for testing purposes.')
  parser.add_argument('-S', '--structures', action='store_true',
    help='Print a list of the unique isoforms')
  parser.add_argument('--struct-human', action='store_true')
  parser.add_argument('-V', '--visualize', nargs='?',
    help='Produce a visualization of the unique structures write the image to this file. '
         'If you omit a filename, it will be displayed in a window.')
  parser.add_argument('-F', '--viz-format', choices=('dot', 'graphviz', 'png'))
  parser.add_argument('-n', '--no-output', dest='output', action='store_false')
  parser.add_argument('-l', '--log', type=argparse.FileType('w'),
    help='Print log messages to this file instead of to stderr. Warning: Will overwrite the file.')
  parser.add_argument('-q', '--quiet', dest='volume', action='store_const', const=logging.CRITICAL)
  parser.add_argument('-i', '--info', dest='volume', action='store_const', const=logging.INFO)
  parser.add_argument('-v', '--verbose', dest='volume', action='store_const', const=VERBOSE)
  parser.add_argument('-D', '--debug', dest='volume', action='store_const', const=logging.DEBUG,
    help='Print debug messages (very verbose).')

  args = parser.parse_args(argv[1:])

  logging.basicConfig(stream=args.log, level=args.volume, format='%(message)s')
  tone_down_logger()

  logging.info('Reading the fasta/q to map read names to barcodes..')
  names_to_barcodes = map_names_to_barcodes(args.reads, args.limit)

  logging.info('Reading the SAM to build the graph of barcode relationships..')
  graph = read_alignments(args.sam, names_to_barcodes, args.pos, args.mapq, args.dist, args.limit)

  logging.info('Reading the families.tsv to get the counts of each family..')
  family_counts = get_family_counts(args.families, args.limit)

  if args.structures:
    logging.info('Counting the unique barcode networks..')
    structures = count_structures(graph, family_counts)
    print_structures(structures, args.struct_human)
    if args.visualize != 0:
      logging.info('Generating a visualization of barcode networks..')
      visualize([s['graph'] for s in structures], args.visualize, args.viz_format)

  logging.info('Building the correction table from the graph..')
  corrections = make_correction_table(graph, family_counts, args.choose_by)

  logging.info('Reading the families.tsv again to print corrected output..')
  families = open_as_text_or_gzip(args.families.name)
  print_corrected_output(families, corrections, args.prepend, args.tag_len, args.limit, args.output)

  max_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
  logging.info('Max memory usage: {:0.2f}MB'.format(max_mem))


def detect_format(reads_file, max_lines=7):
  """Detect whether a file is a fastq or a fasta, based on its content."""
  fasta_votes = 0
  fastq_votes = 0
  line_num = 0
  for line in reads_file:
    line_num += 1
    if line_num % 4 == 1:
      if line.startswith('@'):
        fastq_votes += 1
      elif line.startswith('>'):
        fasta_votes += 1
    elif line_num % 4 == 3:
      if line.startswith('+'):
        fastq_votes += 1
      elif line.startswith('>'):
        fasta_votes += 1
    if line_num >= max_lines:
      break
  reads_file.seek(0)
  if fasta_votes > fastq_votes:
    return 'fasta'
  elif fastq_votes > fasta_votes:
    return 'fastq'
  else:
    return None


def read_fastaq(reads_file):
  filename = reads_file.name
  if filename.endswith('.fa') or filename.endswith('.fasta'):
    format = 'fasta'
  elif filename.endswith('.fq') or filename.endswith('.fastq'):
    format = 'fastq'
  else:
    format = detect_format(reads_file)
  if format == 'fasta':
    return read_fasta(reads_file)
  elif format == 'fastq':
    return read_fastq(reads_file)


def read_fasta(reads_file):
  """Read a FASTA file, yielding read names and sequences.
  NOTE: This assumes sequences are only one line!"""
  line_num = 0
  for line_raw in reads_file:
    line = line_raw.rstrip('\r\n')
    line_num += 1
    if line_num % 2 == 1:
      assert line.startswith('>'), line
      read_name = line[1:]
    elif line_num % 2 == 0:
      read_seq = line
      yield read_name, read_seq


def read_fastq(reads_file):
  """Read a FASTQ file, yielding read names and sequences.
  NOTE: This assumes sequences are only one line!"""
  line_num = 0
  for line in reads_file:
    line_num += 1
    if line_num % 4 == 1:
      assert line.startswith('@'), line
      read_name = line[1:].rstrip('\r\n')
    elif line_num % 4 == 2:
      read_seq = line.rstrip('\r\n')
      yield read_name, read_seq


def map_names_to_barcodes(reads_file, limit=None):
  """Map barcode names to their sequences."""
  names_to_barcodes = {}
  read_num = 0
  for read_name, read_seq in read_fastaq(reads_file):
    read_num += 1
    if limit is not None and read_num > limit:
      break
    try:
      name = int(read_name)
    except ValueError:
      logging.critical('non-int read name "{}"'.format(name))
      raise
    names_to_barcodes[name] = read_seq
  reads_file.close()
  return names_to_barcodes


def parse_alignment(sam_file, pos_thres, mapq_thres, dist_thres):
  """Parse the SAM file and yield reads that pass the filters.
  Returns (qname, rname)."""
  line_num = 0
  for line in sam_file:
    line_num += 1
    if line.startswith('@'):
      logging.debug('Header line ({})'.format(line_num))
      continue
    fields = line.split('\t')
    logging.debug('read {} -> ref {} (read seq {}):'.format(fields[2], fields[0], fields[9]))
    try:
      qname = int(fields[0])
      rname = int(fields[2])
    except ValueError:
      if fields[2] == '*':
        logging.debug('\tRead unmapped (reference == "*")')
        continue
      else:
        logging.error('Non-integer read name(s) on line {}: "{}", "{}".'
                      .format(line_num, qname, rname))
        raise
    # Apply alignment quality filters.
    try:
      flags = int(fields[1])
      pos = int(fields[3])
      mapq = int(fields[4])
    except ValueError:
      logging.warn('\tNon-integer flag ({}), pos ({}), or mapq ({})'
                   .format(fields[1], fields[3], fields[4]))
      continue
    if flags & 4:
      logging.debug('\tRead unmapped (flag & 4 == True)')
      continue
    if abs(pos - 1) > pos_thres:
      logging.debug('\tAlignment failed pos filter: abs({} - 1) > {}'.format(pos, pos_thres))
      continue
    if mapq < mapq_thres:
      logging.debug('\tAlignment failed mapq filter: {} > {}'.format(mapq, mapq_thres))
      continue
    nm = None
    for tag in fields[11:]:
      if tag.startswith('NM:i:'):
        try:
          nm = int(tag[5:])
        except ValueError:
          logging.error('Invalid NM tag "{}" on line {}.'.format(tag, line_num))
          raise
        break
    assert nm is not None, line_num
    if nm > dist_thres:
      logging.debug('\tAlignment failed NM distance filter: {} > {}'.format(nm, dist_thres))
      continue
    yield qname, rname
  sam_file.close()


def read_alignments(sam_file, names_to_barcodes, pos_thres, mapq_thres, dist_thres, limit=None):
  """Read the alignments from the SAM file.
  Returns a dict mapping each reference sequence (RNAME) to sets of sequences (QNAMEs) that align to
  it."""
  graph = networkx.Graph()
  # Maps correct barcode numbers to sets of original barcodes (includes correct ones).
  line_num = 0
  for qname, rname in parse_alignment(sam_file, pos_thres, mapq_thres, dist_thres):
    line_num += 1
    if limit is not None and line_num > limit:
      break
    # Skip self-alignments.
    if rname == qname:
      continue
    rseq = names_to_barcodes[rname]
    qseq = names_to_barcodes[qname]
    graph.add_node(rseq)
    graph.add_node(qseq)
    graph.add_edge(rseq, qseq)
  return graph


def get_family_counts(families_file, limit=None):
  """For each family (barcode), count how many read pairs exist for each strand (order)."""
  family_counts = {}
  last_barcode = None
  this_family_counts = None
  line_num = 0
  for line in families_file:
    line_num += 1
    if limit is not None and line_num > limit:
      break
    fields = line.rstrip('\r\n').split('\t')
    barcode = fields[0]
    order = fields[1]
    if barcode != last_barcode:
      if this_family_counts:
        this_family_counts['all'] = this_family_counts['ab'] + this_family_counts['ba']
      family_counts[last_barcode] = this_family_counts
      this_family_counts = {'ab':0, 'ba':0}
      last_barcode = barcode
    this_family_counts[order] += 1
  this_family_counts['all'] = this_family_counts['ab'] + this_family_counts['ba']
  family_counts[last_barcode] = this_family_counts
  families_file.close()
  return family_counts


def make_correction_table(meta_graph, family_counts, choose_by='reads'):
  """Make a table mapping original barcode sequences to correct barcodes.
  Assumes the most connected node in the graph as the correct barcode."""
  corrections = {}
  for graph in networkx.connected_component_subgraphs(meta_graph):
    if choose_by == 'reads':
      def key(bar):
        return family_counts[bar]['all']
    elif choose_by == 'connectivity':
      degrees = graph.degree()
      def key(bar):
        return degrees[bar]
    barcodes = sorted(graph.nodes(), key=key, reverse=True)
    correct = barcodes[0]
    for barcode in barcodes:
      if barcode != correct:
        logging.debug('Correcting {} ->\n           {}\n'.format(barcode, correct))
        corrections[barcode] = correct
  return corrections


def print_corrected_output(families_file, corrections, prepend=False, tag_len=None,
                           limit=None, output=True):
  # Determine barcode tag length if not given.
  if tag_len is None:
    tag_len = len(corrections.keys()[0])//2
  line_num = 0
  barcode_num = 0
  barcode_last = None
  corrected = {'reads':0, 'barcodes':0}
  reads = [0, 0]
  corrections_in_this_family = 0
  for line in families_file:
    line_num += 1
    if limit is not None and line_num > limit:
      break
    fields = line.rstrip('\r\n').split('\t')
    raw_barcode = fields[0]
    order = fields[1]
    if raw_barcode != barcode_last:
      # We just started a new family.
      barcode_num += 1
      family_info = '{}\t{}\t{}'.format(barcode_last, reads[0], reads[1])
      if corrections_in_this_family:
        corrected['reads'] += corrections_in_this_family
        corrected['barcodes'] += 1
        family_info += '\tCORRECTED!'
      else:
        family_info += '\tuncorrected'
      logging.log(VERBOSE, family_info)
      reads = [0, 0]
      corrections_in_this_family = 0
      barcode_last = raw_barcode
    if order == 'ab':
      reads[0] += 1
    elif order == 'ba':
      reads[1] += 1
    if raw_barcode in corrections:
      correct_barcode = corrections[raw_barcode]
      corrections_in_this_family += 1
    else:
      correct_barcode = raw_barcode
    if prepend:
      fields.insert(0, correct_barcode)
    else:
      fields[0] = correct_barcode
    if output:
      print(*fields, sep='\t')
  families_file.close()
  if corrections_in_this_family:
    corrected['reads'] += corrections_in_this_family
    corrected['barcodes'] += 1
  logging.info('Corrected {barcodes} barcodes on {reads} read pairs.'.format(**corrected))


def count_structures(meta_graph, family_counts):
  """Count the number of unique (isomorphic) subgraphs in the main graph."""
  structures = []
  for graph in networkx.connected_component_subgraphs(meta_graph):
    match = False
    for structure in structures:
      archetype = structure['graph']
      if networkx.is_isomorphic(graph, archetype):
        match = True
        structure['count'] += 1
        structure['central'] += int(is_centralized(graph, family_counts))
        break
    if not match:
      size = len(graph)
      central = is_centralized(graph, family_counts)
      structures.append({'graph':graph, 'size':size, 'count':1, 'central':int(central)})
  return structures


def is_centralized(graph, family_counts):
  """Checks if the graph is centralized in terms of where the reads are located.
  In a centralized graph, the node with the highest degree is the only one which (may) have more
  than one read pair associated with that barcode.
  This returns True if that's the case, False otherwise."""
  if len(graph) == 2:
    # Special-case graphs with 2 nodes, since the other algorithm doesn't work for them.
    # - When both nodes have a degree of 1, sorting by degree doesn't work and can result in the
    #   barcode with more read pairs coming second.
    barcode1, barcode2 = graph.nodes()
    counts1 = family_counts[barcode1]
    counts2 = family_counts[barcode2]
    total1 = counts1['all']
    total2 = counts2['all']
    logging.debug('{}: {:3d} ({}/{})\n{}: {:3d} ({}/{})\n'
                  .format(barcode1, total1, counts1['ab'], counts1['ba'],
                          barcode2, total2, counts2['ab'], counts2['ba']))
    if (total1 >= 1 and total2 == 1) or (total1 == 1 and total2 >= 1):
      return True
    else:
      return False
  else:
    degrees = graph.degree()
    first = True
    for barcode in sorted(graph.nodes(), key=lambda barcode: degrees[barcode], reverse=True):
      if not first:
        counts = family_counts[barcode]
        # How many read pairs are associated with this barcode (how many times did we see this barcode)?
        try:
          if counts['all'] > 1:
            return False
        except TypeError:
          logging.critical('barcode: {}, counts: {}'.format(barcode, counts))
          raise
      first = False
    return True


def print_structures(structures, human=True):
  # Define a cmp function to sort the list of structures in ascending order of size, but then
  # descending order of count.
  def cmp_fxn(structure1, structure2):
    if structure1['size'] == structure2['size']:
      return structure2['count'] - structure1['count']
    else:
      return structure1['size'] - structure2['size']
  width = None
  last_size = None
  for structure in sorted(structures, cmp=cmp_fxn):
    size = structure['size']
    graph = structure['graph']
    if size == last_size:
      i += 1
    else:
      i = 0
    if width is None:
      width = str(len(str(structure['count'])))
    letters = num_to_letters(i)
    # node_data = get_node_data(graph, graph.degree(), family_counts)
    # node_data_str = ['{degree}:{count1}/{count2}'.format(**datum) for datum in node_data]
    degrees = sorted(graph.degree().values(), reverse=True)
    if human:
      degrees_str = ' '.join(map(str, degrees))
    else:
      degrees_str = ','.join(map(str, degrees))
    if human:
      format_str = '{:2d}{:<3s} {count:<'+width+'d} {central:<'+width+'d} {}'
      print(format_str.format(size, letters+':', degrees_str, **structure))
    else:
      print(size, letters, structure['count'], structure['central'], degrees_str, sep='\t')
    last_size = size


def num_to_letters(i):
  """Translate numbers to letters, e.g. 1 -> A, 10 -> K, 100 -> CW
  Note: Can't handle numbers over 701."""
  if i < 26:
    return chr(65+i)
  else:
    x = (i // 26) - 1
    y = i % 26
    return chr(65+x)+chr(65+y)


def get_node_data(graph, degrees, family_counts):
  node_data = []
  for barcode, degree in degrees.items():
    counts = family_counts[barcode]
    node_datum = {'degree':degree, 'count1':counts['ab'], 'count2':counts['ba']}
    node_data.append(node_datum)
  return sorted(node_data, key=lambda datum: datum['degree'], reverse=True)


def visualize(graphs, viz_path, args_viz_format):
    import matplotlib
    from networkx.drawing.nx_agraph import graphviz_layout
    from networkx.drawing.nx_pydot import write_dot
    meta_graph = networkx.Graph()
    for graph in graphs:
      add_graph(meta_graph, graph)
    pos = graphviz_layout(meta_graph)
    networkx.draw(meta_graph, pos)
    if viz_path:
      ext = os.path.splitext(viz_path)[1]
      if ext == '.dot':
        viz_format = 'graphviz'
      elif ext == '.png':
        viz_format = 'png'
    else:
      viz_format = args_viz_format
    if viz_format == 'graphviz':
      assert viz_path is not None, 'Must provide a filename to --visualize if using --viz-format "graphviz".'
      base_path = os.path.splitext(viz_path)
      write_dot(meta_graph, base_path+'.dot')
      run_command('dot', '-T', 'png', '-o', base_path+'.png', base_path+'.dot')
      logging.info('Wrote image of graph to '+base_path+'.dot')
    elif viz_format == 'png':
      if viz_path is None:
        matplotlib.pyplot.show()
      else:
        matplotlib.pyplot.savefig(viz_path)


def add_graph(graph, subgraph):
  # I'm sure there's a function in the library for this, but just cause I need it quick..
  for node in subgraph.nodes():
    graph.add_node(node)
  for edge in subgraph.edges():
    graph.add_edge(*edge)
  return graph


def open_as_text_or_gzip(path):
  """Return an open file-like object reading the path as a text file or a gzip file, depending on
  which it looks like."""
  if detect_gzip(path):
    return gzip.open(path, 'r')
  else:
    return open(path, 'rU')


def detect_gzip(path):
  """Return True if the file looks like a gzip file: ends with .gz or contains non-ASCII bytes."""
  ext = os.path.splitext(path)[1]
  if ext == '.gz':
    return True
  elif ext in ('.txt', '.tsv', '.csv'):
    return False
  with open(path) as fh:
    is_not_ascii = detect_non_ascii(fh.read(100))
  if is_not_ascii:
    return True


def detect_non_ascii(bytes, max_test=100):
  """Return True if any of the first "max_test" bytes are non-ASCII (the high bit set to 1).
  Return False otherwise."""
  for i, char in enumerate(bytes):
    # Is the high bit a 1?
    if ord(char) & 128:
      return True
    if i >= max_test:
      return False
  return False


def run_command(*command):
  try:
    exit_status = subprocess.call(command)
  except subprocess.CalledProcessError as cpe:
    exit_status = cpe.returncode
  except OSError:
    exit_status = None
  return exit_status


def tone_down_logger():
  """Change the logging level names from all-caps to capitalized lowercase.
  E.g. "WARNING" -> "Warning" (turn down the volume a bit in your log files)"""
  for level in (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG):
    level_name = logging.getLevelName(level)
    logging.addLevelName(level, level_name.capitalize())


if __name__ == '__main__':
  sys.exit(main(sys.argv))
