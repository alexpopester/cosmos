# encoding: ascii-8bit

# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE.md for more details.

# Round-trip regression harness for the XTCE converter / parser pair.
#
# Each example takes a COSMOS configuration, exports it to XTCE (XtceConverter),
# re-imports the generated XTCE (XtceParser), and asserts that the reconstructed
# Packet matches the original. The goal is lossless COSMOS -> XTCE -> COSMOS: any
# field that silently changes or disappears is a round-trip defect, and this file
# is where such defects become reproducible failures rather than code review notes.
#
# When a construct can't be expressed natively in XTCE (named limits sets, green
# limits, string / ANY states, variable length strings), the converter writes a
# COSMOS_* AncillaryData carrier that the parser reads back, so the round trip stays
# lossless while the generated XTCE remains valid for foreign tools. New round-trip
# gaps should be added here first as a failing example, then fixed.

require 'spec_helper'
require 'openc3'
require 'openc3/packets/packet_config'
require 'openc3/packets/parsers/xtce_converter'
require 'tempfile'
require 'fileutils'

module OpenC3
  describe 'XTCE round trip' do
    before(:all) do
      setup_system()
    end

    # Export the given PacketConfig to XTCE under a temp dir, then parse the
    # generated file for `target` back into a fresh PacketConfig and return it.
    def round_trip(pc, target, time_association_name = 'PACKET_TIME')
      dir = Dir.mktmpdir('xtce_rt')
      @dirs ||= []
      @dirs << dir
      pc.to_xtce(dir, time_association_name)
      xml_path = File.join(dir, target, 'cmd_tlm', target.downcase + '.xtce')
      expect(File).to exist(xml_path), "converter did not produce #{xml_path}"
      # Every generated file must validate against the vendored XTCE 1.2 schema.
      errors = XtceConverter.schema_errors(xml_path)
      expect(errors).to be_empty, "XTCE 1.2 schema validation errors:\n#{errors.join("\n")}"
      reparsed = PacketConfig.new
      reparsed.process_file(xml_path, target)
      [reparsed, xml_path]
    end

    # Build a PacketConfig from COSMOS configuration text.
    def config_from(text)
      tf = Tempfile.new(['rt_config', '.txt'])
      tf.write(text)
      tf.close
      @files ||= []
      @files << tf
      pc = PacketConfig.new
      pc.process_file(tf.path, 'TGT')
      pc
    end

    after(:each) do
      (@files || []).each { |f| f.unlink rescue nil }
      (@dirs || []).each { |d| FileUtils.rm_rf(d) rescue nil }
      @files = []
      @dirs = []
    end

    # Compare the round-trip-significant fields of two items. `check_default` and
    # `check_range` are only meaningful for command parameters.
    def expect_item_preserved(original, result, check_default: false, check_range: false)
      aggregate_failures("item #{original.name}") do
        expect(result.data_type).to eq(original.data_type), "data_type"
        expect(result.bit_size).to eq(original.bit_size), "bit_size"
        expect(result.array_size).to eq(original.array_size), "array_size"
        # Strings and Blocks carry no meaningful endianness
        if original.data_type != :STRING && original.data_type != :BLOCK
          expect(result.endianness).to eq(original.endianness), "endianness"
        end
        expect(result.states).to eq(original.states), "states"
        expect(result.units).to eq(original.units), "units"
        expect(result.units_full).to eq(original.units_full), "units_full"
        expect(result.description).to eq(original.description), "description"
        expect(result.default).to eq(original.default), "default" if check_default
        expect(result.range).to eq(original.range), "range" if check_range
      end
    end

    # Walk every non-reserved item in two packets and compare.
    def expect_packet_items_preserved(original, result, **opts)
      original.sorted_items.each do |item|
        next if Packet::RESERVED_ITEM_NAMES.include?(item.name)
        found = result.get_item(item.name)
        expect(found).not_to be_nil, "item #{item.name} missing after round trip"
        expect_item_preserved(item, found, **opts)
      end
    end

    # Whole-packet lossless comparison. Packet#to_config is COSMOS's own canonical
    # serializer: it emits every field COSMOS can represent - each item's
    # bit_offset / bit_size / array_size (so size and layout are covered), endianness,
    # states, conversions, limits, units, format, meta, and the packet-level constructs
    # (accessor, processors, hazardous, ...). So an empty diff of the two configs means
    # the round trip lost nothing to_config can express - a far stronger check than any
    # hand-picked field list. Size is called out explicitly up front, and items present
    # in only one side are reported by name. Returns readable difference strings.
    #
    # Note: fields to_config itself does not serialize (KEY, OVERLAP, and a conversion's
    # constructor args) are invisible to this check - those are COSMOS-core gaps, not
    # XTCE round-trip gaps, and are documented separately.
    #
    # Delegates to XtceConverter.packet_differences so the spec and the xtce_compare CLI
    # share one comparison implementation.
    def packet_config_diff(original, reparsed, cmd_or_tlm)
      XtceConverter.packet_differences(original, reparsed, cmd_or_tlm)
    end

    def expect_lossless(original, reparsed, cmd_or_tlm)
      diffs = packet_config_diff(original, reparsed, cmd_or_tlm)
      expect(diffs).to be_empty, "round trip is not lossless:\n#{diffs.join("\n")}"
    end

    # ------------------------------------------------------------------ #
    # Telemetry
    # ------------------------------------------------------------------ #
    describe "telemetry data types" do
      it "preserves signed/unsigned integers of several widths" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM U8 8 UINT "u8"
            APPEND_ITEM S8 8 INT "s8"
            APPEND_ITEM U16 16 UINT "u16"
            APPEND_ITEM S16 16 INT "s16"
            APPEND_ITEM U24 24 UINT "u24"
            APPEND_ITEM U32 32 UINT "u32"
            APPEND_ITEM S32 32 INT "s32"
            APPEND_ITEM U64 64 UINT "u64"
            APPEND_ITEM S64 64 INT "s64"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        expect_packet_items_preserved(original, reparsed.telemetry['TGT']['PKT'])
      end

      it "preserves 32 and 64 bit floats" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM F32 32 FLOAT "f32"
            APPEND_ITEM F64 64 FLOAT "f64"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        expect_packet_items_preserved(original, reparsed.telemetry['TGT']['PKT'])
      end

      it "preserves little endian multi-byte items" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT LITTLE_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM W16 16 UINT "w16"
            APPEND_ITEM W32 32 UINT "w32"
            APPEND_ITEM FL 32 FLOAT "float le"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        expect_packet_items_preserved(original, reparsed.telemetry['TGT']['PKT'])
      end

      it "preserves a mix of endianness within one packet" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM BE 16 UINT "big"
            APPEND_ITEM LE 16 UINT "little" LITTLE_ENDIAN
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('BE').endianness).to eq(:BIG_ENDIAN)
        expect(result.get_item('LE').endianness).to eq(:LITTLE_ENDIAN)
      end

      it "preserves fixed length strings and blocks" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM NAME 128 STRING "name"
            APPEND_ITEM BLK 32 BLOCK "block"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        expect_packet_items_preserved(original, reparsed.telemetry['TGT']['PKT'])
      end

      it "preserves a variable length string size" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM STR 0 STRING "variable name"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('STR').bit_size).to eq(0)
      end

      it "preserves a variable length block size" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM DATA 0 BLOCK "variable block"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('DATA').bit_size).to eq(0)
      end
    end

    describe "telemetry arrays" do
      it "preserves an integer array" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ARRAY_ITEM ARY 8 UINT 80 "array of 10 bytes"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('ARY').array_size).to eq(original.get_item('ARY').array_size)
        expect(result.get_item('ARY').bit_size).to eq(original.get_item('ARY').bit_size)
      end

      it "preserves a float array" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ARRAY_ITEM ARY 64 FLOAT 640 "array of 10 doubles"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('ARY').array_size).to eq(640)
        expect(result.get_item('ARY').bit_size).to eq(64)
        expect(result.get_item('ARY').data_type).to eq(:FLOAT)
      end
    end

    describe "telemetry conversions and units" do
      it "preserves a polynomial read conversion" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM RAW 16 UINT "raw"
              POLY_READ_CONVERSION -100.0 0.00305
        CFG
        reparsed, = round_trip(pc, 'TGT')
        conv = reparsed.telemetry['TGT']['PKT'].get_item('RAW').read_conversion
        expect(conv).to be_a(PolynomialConversion)
        expect(conv.coeffs).to eq([-100.0, 0.00305])
      end

      it "preserves units" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP 16 UINT "temp"
              UNITS Celsius C
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('TEMP').units).to eq(original.get_item('TEMP').units)
        expect(result.get_item('TEMP').units_full).to eq(original.get_item('TEMP').units_full)
      end

      it "preserves a generic read conversion on a non-derived item" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM RAW 16 UINT "raw"
              GENERIC_READ_CONVERSION_START FLOAT 32
                value * 2.0 + 1.0
              GENERIC_READ_CONVERSION_END
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        conv = result.get_item('RAW').read_conversion
        expect(conv).to be_a(GenericConversion)
        # Serialized config is the strongest lossless check for the conversion.
        expect(result.get_item('RAW').to_config(:TELEMETRY, :BIG_ENDIAN)).to eq(
          original.get_item('RAW').to_config(:TELEMETRY, :BIG_ENDIAN)
        )
      end

      it "preserves a segmented polynomial read conversion" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM RAW 16 UINT "raw"
              SEG_POLY_READ_CONVERSION 0 10.0 0.5
              SEG_POLY_READ_CONVERSION 100 20.0 1.5
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('RAW').read_conversion).to be_a(SegmentedPolynomialConversion)
        expect(result.get_item('RAW').to_config(:TELEMETRY, :BIG_ENDIAN)).to eq(
          original.get_item('RAW').to_config(:TELEMETRY, :BIG_ENDIAN)
        )
      end
    end

    describe "telemetry format strings" do
      it "preserves a FORMAT_STRING" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM VOLTAGE 16 UINT "voltage"
              FORMAT_STRING "0x%04X"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('VOLTAGE').format_string).to eq(original.get_item('VOLTAGE').format_string)
      end
    end

    describe "telemetry booleans" do
      it "round-trips a BOOL item (data type preserved via carrier)" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM FLAG 8 BOOL "bool item"
            APPEND_ITEM WIDEFLAG 16 BOOL "wide bool"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        flag = result.get_item('FLAG')
        expect(flag.data_type).to eq(:BOOL)
        expect(flag.bit_size).to eq(8)
        expect(flag.states).to be_nil
        expect(result.get_item('WIDEFLAG').data_type).to eq(:BOOL)
        expect(result.get_item('WIDEFLAG').bit_size).to eq(16)
        # to_config is clean for telemetry BOOL, so use it as the lossless check.
        expect(flag.to_config(:TELEMETRY, :BIG_ENDIAN)).to eq(original.get_item('FLAG').to_config(:TELEMETRY, :BIG_ENDIAN))
      end
    end

    describe "telemetry opaque data types (ARRAY, OBJECT, ANY)" do
      it "round-trips ARRAY, OBJECT and ANY items via the data-type carrier" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM ARR 8 ARRAY "complex array"
            APPEND_ITEM OBJ 8 OBJECT "object item"
            APPEND_ITEM ANYTHING 8 ANY "any item"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        { 'ARR' => :ARRAY, 'OBJ' => :OBJECT, 'ANYTHING' => :ANY }.each do |name, dtype|
          item = result.get_item(name)
          expect(item.data_type).to eq(dtype), "#{name} data_type"
          expect(item.bit_size).to eq(8), "#{name} bit_size"
          expect(item.to_config(:TELEMETRY, :BIG_ENDIAN)).to eq(
            original.get_item(name).to_config(:TELEMETRY, :BIG_ENDIAN)
          ), "#{name} to_config"
        end
      end
    end

    describe "telemetry item metadata" do
      it "preserves item META" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM VALUE 16 UINT "value"
              META TEST "VALUE1" "VALUE2"
              META SINGLE "ONE"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('VALUE').meta).to eq(original.get_item('VALUE').meta)
      end

      it "preserves the OBFUSCATE flag" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM SECRET 16 UINT "secret"
              OBFUSCATE
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('SECRET').obfuscate).to be true
      end
    end

    describe "telemetry states (enumerations)" do
      it "preserves integer enumerated states" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM FLAG 8 UINT "flag"
              STATE OFF 0
              STATE ON 1
              STATE FAULT 255
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('FLAG').states).to eq(original.get_item('FLAG').states)
      end

      it "preserves negative enumerated state values" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM MODE 8 INT "mode"
              STATE LOW -1
              STATE ZERO 0
              STATE HIGH 1
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('MODE').states).to eq(original.get_item('MODE').states)
      end

      it "preserves string enumerated states" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM CMD 2048 STRING "enumerated string"
              STATE "NOOP" "NOOP"
              STATE "FIRE" "FIRE"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('CMD').states).to eq(original.get_item('CMD').states)
      end

      it "preserves state colors" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM STATUS 8 UINT "status"
              STATE CONNECTED 1 GREEN
              STATE UNAVAILABLE 0 YELLOW
              STATE FAULT 2 RED
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('STATUS').states).to eq(original.get_item('STATUS').states)
        expect(result.get_item('STATUS').state_colors).to eq({ 'CONNECTED' => :GREEN, 'UNAVAILABLE' => :YELLOW, 'FAULT' => :RED })
      end

      it "preserves an ANY catch-all state" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM FLAG 8 UINT "flag"
              STATE OFF 0
              STATE ON 1
              STATE ERROR ANY
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('FLAG').states).to eq(original.get_item('FLAG').states)
      end
    end

    describe "telemetry limits" do
      it "preserves the DEFAULT red/yellow limits" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP 16 INT "temp"
              LIMITS DEFAULT 1 ENABLED -80 -70 60 80
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('TEMP').limits.values[:DEFAULT]).to eq([-80.0, -70.0, 60.0, 80.0])
      end

      it "preserves DEFAULT green (blue) limits" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP 16 INT "temp"
              LIMITS DEFAULT 1 ENABLED -80 -70 60 80 -20 20
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('TEMP').limits.values[:DEFAULT]).to eq([-80.0, -70.0, 60.0, 80.0, -20.0, 20.0])
      end

      it "preserves named (non-DEFAULT) limits sets" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP 16 INT "temp"
              LIMITS DEFAULT 1 ENABLED -80 -70 60 80
              LIMITS TVAC 1 ENABLED -60 -50 40 50
        CFG
        reparsed, = round_trip(pc, 'TGT')
        values = reparsed.telemetry['TGT']['PKT'].get_item('TEMP').limits.values
        expect(values[:DEFAULT]).to eq([-80.0, -70.0, 60.0, 80.0])
        expect(values[:TVAC]).to eq([-60.0, -50.0, 40.0, 50.0])
      end

      it "preserves named sets together with green limits" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP 16 INT "temp"
              LIMITS DEFAULT 1 ENABLED -80 -70 60 80 -20 20
              LIMITS TVAC 3 ENABLED -60 -50 40 50
        CFG
        reparsed, = round_trip(pc, 'TGT')
        values = reparsed.telemetry['TGT']['PKT'].get_item('TEMP').limits.values
        expect(values[:DEFAULT]).to eq([-80.0, -70.0, 60.0, 80.0, -20.0, 20.0])
        expect(values[:TVAC]).to eq([-60.0, -50.0, 40.0, 50.0])
      end

      it "preserves the disabled limits state" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP 16 INT "temp"
              LIMITS DEFAULT 1 DISABLED -80 -70 60 80
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('TEMP').limits.enabled).to be false
      end

      it "preserves a non-default persistence setting" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP 16 INT "temp"
              LIMITS DEFAULT 5 ENABLED -80 -70 60 80
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('TEMP').limits.persistence_setting).to eq(5)
      end

      it "preserves a LIMITS_RESPONSE class" do
        # The response is a class reference (its constructor args are not serialized by
        # COSMOS's own to_config, same as conversions), so the class must be loadable at
        # import time. require_class returns an already-loaded class without the file, so
        # defining it here is enough.
        response_file = File.join(File.dirname(__FILE__), "../../rt_limits_response.rb")
        File.open(response_file, 'w') do |file|
          file.puts "require 'openc3/packets/limits_response'"
          file.puts "class RtLimitsResponse < OpenC3::LimitsResponse"
          file.puts "  def call(target_name, packet_name, item, old_limits_state, new_limits_state)"
          file.puts "  end"
          file.puts "end"
        end
        saved_verbose = $VERBOSE
        $VERBOSE = nil
        load 'rt_limits_response.rb'
        $VERBOSE = saved_verbose

        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP 16 INT "temp"
              LIMITS DEFAULT 1 ENABLED -80 -70 60 80
              LIMITS_RESPONSE rt_limits_response.rb
        CFG
        reparsed, = round_trip(pc, 'TGT')
        response = reparsed.telemetry['TGT']['PKT'].get_item('TEMP').limits.response
        expect(response).to be_a(RtLimitsResponse)
      ensure
        File.delete(response_file) if response_file && File.exist?(response_file)
      end
    end

    describe "telemetry packet-level features" do
      it "preserves short buffer allowed (ALLOW_SHORT)" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            ALLOW_SHORT
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM VALUE 16 UINT "value"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].short_buffer_allowed).to be true
      end

      it "preserves multiple packets and their ID values" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT1 BIG_ENDIAN "Packet 1"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM A 16 UINT "a"
          TELEMETRY TGT PKT2 BIG_ENDIAN "Packet 2"
            APPEND_ID_ITEM ID 8 UINT 2 "id"
            APPEND_ITEM B 16 UINT "b"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT1'].get_item('ID').id_value).to eq(1)
        expect(reparsed.telemetry['TGT']['PKT2'].get_item('ID').id_value).to eq(2)
      end

      it "preserves multiple ID items in one packet" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM APID 8 UINT 1 "apid"
            APPEND_ID_ITEM TYPE 8 UINT 2 "type"
            APPEND_ITEM VALUE 16 UINT "value"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('APID').id_value).to eq(1)
        expect(result.get_item('TYPE').id_value).to eq(2)
      end

      it "preserves names containing characters XTCE forbids" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM ARRAY[0].VALUE/RAW 16 UINT "weird name"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('ARRAY[0].VALUE/RAW')).not_to be_nil
      end
    end

    describe "telemetry derived items" do
      it "round-trips a derived item with an inline conversion" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM TEMP1 16 UINT "t1"
            APPEND_ITEM TEMP2 16 UINT "t2"
            ITEM TEMPAVG 0 0 DERIVED "Average temperature"
              GENERIC_READ_CONVERSION_START FLOAT 32
                (packet.read('TEMP1') + packet.read('TEMP2')) / 2.0
              GENERIC_READ_CONVERSION_END
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        derived = result.get_item('TEMPAVG')
        expect(derived).not_to be_nil
        expect(derived.data_type).to eq(:DERIVED)
        expect(derived.bit_size).to eq(0)
        expect(derived.read_conversion).to be_a(GenericConversion)
        # The config a derived item serializes to is the strongest lossless check
        expect(derived.to_config(:TELEMETRY, :BIG_ENDIAN)).to eq(original.get_item('TEMPAVG').to_config(:TELEMETRY, :BIG_ENDIAN))
      end

      it "round-trips a derived item carrying states, units and limits" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM RAW 16 UINT "raw"
            ITEM DERIV 0 0 DERIVED "Derived value"
              GENERIC_READ_CONVERSION_START UINT 8
                packet.read('RAW') % 3
              GENERIC_READ_CONVERSION_END
              STATE OFF 0
              STATE ON 1
              STATE FAULT 2
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('DERIV').to_config(:TELEMETRY, :BIG_ENDIAN)).to eq(original.get_item('DERIV').to_config(:TELEMETRY, :BIG_ENDIAN))
      end

      it "round-trips a derived time-association item and stays schema valid" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM SECONDS 32 UINT "seconds"
            ITEM PACKET_TIME 0 0 DERIVED "Packet time"
              GENERIC_READ_CONVERSION_START
                Time.at(packet.read('SECONDS'))
              GENERIC_READ_CONVERSION_END
        CFG
        # round_trip validates the generated file against the XTCE 1.2 schema, which
        # would flag a dangling TimeAssociation reference indirectly via reimport.
        reparsed, = round_trip(pc, 'TGT', 'PACKET_TIME')
        expect(reparsed.telemetry['TGT']['PKT'].get_item('PACKET_TIME').data_type).to eq(:DERIVED)
      end
    end

    describe "telemetry fixed offsets (non-appended)" do
      it "preserves explicit bit offsets" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Packet"
            ID_ITEM ID 0 8 UINT 1 "id"
            ITEM A 16 16 UINT "a at bit 16"
            ITEM B 32 16 UINT "b at bit 32"
        CFG
        original = pc.telemetry['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['PKT']
        expect(result.get_item('A').bit_offset).to eq(original.get_item('A').bit_offset)
        expect(result.get_item('B').bit_offset).to eq(original.get_item('B').bit_offset)
      end
    end

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    describe "command arguments" do
      it "preserves integer args with ranges, defaults and states" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER LEVEL 16 UINT 0 1000 500 "level"
            APPEND_PARAMETER SIGNED 16 INT -100 100 -5 "signed"
            APPEND_PARAMETER MODE 8 UINT 0 1 0 "mode"
              STATE SAFE 0
              STATE ACTIVE 1
        CFG
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.commands['TGT']['PKT']
        expect(result.get_item('LEVEL').range).to eq(0..1000)
        expect(result.get_item('LEVEL').default).to eq(500)
        expect(result.get_item('SIGNED').range).to eq(-100..100)
        expect(result.get_item('SIGNED').default).to eq(-5)
        expect(result.get_item('MODE').states).to eq({ 'SAFE' => 0, 'ACTIVE' => 1 })
      end

      it "preserves 64 bit MIN/MAX ranges" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER BIGINT 64 UINT MIN MAX 0 "uint64"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.commands['TGT']['PKT']
        expect(result.get_item('BIGINT').range).to eq(0..(2**64 - 1))
      end

      it "preserves float args" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER GAIN 32 FLOAT -10.5 10.5 1.5 "gain"
            APPEND_PARAMETER BIGF 64 FLOAT MIN MAX 0.0 "double"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.commands['TGT']['PKT']
        expect(result.get_item('GAIN').default).to eq(1.5)
        expect(result.get_item('GAIN').range).to eq(-10.5..10.5)
      end

      it "preserves little endian command arguments" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT LITTLE_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER WORD 16 UINT 0 65535 0 "le word"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.commands['TGT']['PKT'].get_item('WORD').endianness).to eq(:LITTLE_ENDIAN)
      end

      it "preserves string and block args with defaults" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER NAME 64 STRING "hello" "string arg"
            APPEND_PARAMETER BIN 32 BLOCK 0xDEADBEEF "block arg"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.commands['TGT']['PKT']
        expect(result.get_item('NAME').default).to eq("hello")
        expect(result.get_item('BIN').default).to eq("\xDE\xAD\xBE\xEF".b)
      end

      it "preserves a string default that begins with 0x" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER ASCII 80 STRING "0xDEADBEEF" "ascii literal"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.commands['TGT']['PKT'].get_item('ASCII').default).to eq("0xDEADBEEF")
      end

      it "preserves a polynomial write conversion" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER RAW 16 UINT 0 65535 0 "raw"
              POLY_WRITE_CONVERSION 0 2
        CFG
        reparsed, = round_trip(pc, 'TGT')
        conv = reparsed.commands['TGT']['PKT'].get_item('RAW').write_conversion
        expect(conv).to be_a(PolynomialConversion)
        expect(conv.coeffs).to eq([0.0, 2.0])
      end

      it "preserves a non-numeric default on a numeric arg with a write conversion" do
        # A UINT whose default is an IP string, mapped to an int by a write conversion.
        # XTCE can't hold "127.0.0.1" as an integer initialValue, so it rides COSMOS_DEFAULT.
        conv_file = File.join(File.dirname(__FILE__), "../../rt_ip_write_conversion.rb")
        File.open(conv_file, 'w') do |file|
          file.puts "require 'openc3/conversions/conversion'"
          file.puts "class RtIpWriteConversion < OpenC3::Conversion"
          file.puts "  def initialize; super(); @converted_type = :UINT; @converted_bit_size = 32; end"
          file.puts "  def call(value, packet, buffer)"
          file.puts "    Integer(IPAddr.new(value).to_i) rescue 0"
          file.puts "  end"
          file.puts "end"
        end
        saved_verbose = $VERBOSE
        $VERBOSE = nil
        load 'rt_ip_write_conversion.rb'
        $VERBOSE = saved_verbose

        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER IP 32 UINT MIN MAX "127.0.0.1" "ip address"
              WRITE_CONVERSION rt_ip_write_conversion.rb
        CFG
        reparsed, = round_trip(pc, 'TGT')
        ip = reparsed.commands['TGT']['PKT'].get_item('IP')
        expect(ip.data_type).to eq(:UINT)
        expect(ip.bit_size).to eq(32)
        expect(ip.default).to eq("127.0.0.1")
        expect(ip.write_conversion).to be_a(RtIpWriteConversion)
      ensure
        File.delete(conv_file) if conv_file && File.exist?(conv_file)
      end

      it "preserves a generic write conversion on an argument" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER RAW 16 UINT 0 65535 0 "raw"
              GENERIC_WRITE_CONVERSION_START UINT 16
                (value - 1.0) / 2.0
              GENERIC_WRITE_CONVERSION_END
        CFG
        original = pc.commands['TGT']['PKT']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.commands['TGT']['PKT']
        expect(result.get_item('RAW').write_conversion).to be_a(GenericConversion)
        expect(result.get_item('RAW').to_config(:COMMAND, :BIG_ENDIAN)).to eq(
          original.get_item('RAW').to_config(:COMMAND, :BIG_ENDIAN)
        )
      end

      it "round-trips a BOOL argument with a boolean default" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER ENABLE 8 BOOL true "enable flag"
            APPEND_PARAMETER DISABLE 16 BOOL false "disable flag"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.commands['TGT']['PKT']
        enable = result.get_item('ENABLE')
        expect(enable.data_type).to eq(:BOOL)
        expect(enable.bit_size).to eq(8)
        expect(enable.default).to be(true)
        disable = result.get_item('DISABLE')
        expect(disable.data_type).to eq(:BOOL)
        expect(disable.bit_size).to eq(16)
        expect(disable.default).to be(false)
      end

      it "preserves the REQUIRED flag and a non-default OVERFLOW" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER LEVEL 16 UINT 0 1000 500 "level"
              REQUIRED
              OVERFLOW TRUNCATE
        CFG
        reparsed, = round_trip(pc, 'TGT')
        level = reparsed.commands['TGT']['PKT'].get_item('LEVEL')
        expect(level.required).to be true
        expect(level.overflow).to eq(:TRUNCATE)
      end

      it "preserves an array argument" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_ARRAY_PARAMETER ARY 8 UINT 80 "array arg"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.commands['TGT']['PKT']
        expect(result.get_item('ARY').array_size).to eq(80)
        expect(result.get_item('ARY').bit_size).to eq(8)
      end

      it "preserves hazardous and disabled-message states" do
        pc = config_from(<<~CFG)
          COMMAND TGT PKT BIG_ENDIAN "Command"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER MODE 8 UINT 0 2 0 "mode"
              STATE SAFE 0
              STATE FIRE 1 HAZARDOUS "laser will fire"
              STATE QUIET 2 DISABLE_MESSAGES
        CFG
        reparsed, = round_trip(pc, 'TGT')
        mode = reparsed.commands['TGT']['PKT'].get_item('MODE')
        expect(mode.states).to eq({ 'SAFE' => 0, 'FIRE' => 1, 'QUIET' => 2 })
        expect(mode.hazardous).to eq({ 'FIRE' => 'laser will fire' })
        expect(mode.messages_disabled).to eq({ 'QUIET' => true })
      end

      it "preserves multiple commands" do
        pc = config_from(<<~CFG)
          COMMAND TGT CMD1 BIG_ENDIAN "Command 1"
            APPEND_ID_PARAMETER OPCODE 8 UINT 1 1 1 "opcode"
            APPEND_PARAMETER A 16 UINT 0 100 1 "a"
          COMMAND TGT CMD2 BIG_ENDIAN "Command 2"
            APPEND_ID_PARAMETER OPCODE 8 UINT 2 2 2 "opcode"
            APPEND_PARAMETER B 16 UINT 0 100 2 "b"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(reparsed.commands['TGT']['CMD1'].get_item('OPCODE').id_value).to eq(1)
        expect(reparsed.commands['TGT']['CMD2'].get_item('OPCODE').id_value).to eq(2)
      end
    end

    # ------------------------------------------------------------------ #
    # Combined / interaction cases
    # ------------------------------------------------------------------ #
    describe "combined feature interactions" do
      it "round-trips a packet exercising many features at once" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT KITCHEN BIG_ENDIAN "Kitchen sink packet"
            ALLOW_SHORT
            APPEND_ID_ITEM APID 8 UINT 1 "apid"
            APPEND_ITEM COUNTER 32 UINT "counter" LITTLE_ENDIAN
            APPEND_ITEM TEMP 16 UINT "temperature"
              POLY_READ_CONVERSION -100.0 0.00305
              UNITS Celsius C
              LIMITS DEFAULT 1 ENABLED -80 -70 60 80 -20 20
              LIMITS TVAC 1 ENABLED -60 -50 40 50
            APPEND_ITEM MODE 8 UINT "mode"
              STATE OFF 0
              STATE ON 1
              STATE ERROR ANY
            APPEND_ARRAY_ITEM SAMPLES 16 INT 160 "ten signed samples"
            APPEND_ITEM LABEL 64 STRING "label"
            APPEND_ITEM PAYLOAD 0 BLOCK "variable payload"
        CFG
        original = pc.telemetry['TGT']['KITCHEN']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['KITCHEN']

        expect(result.short_buffer_allowed).to be true
        expect(result.get_item('COUNTER').endianness).to eq(:LITTLE_ENDIAN)

        temp = result.get_item('TEMP')
        expect(temp.read_conversion).to be_a(PolynomialConversion)
        expect(temp.read_conversion.coeffs).to eq([-100.0, 0.00305])
        expect(temp.units).to eq(original.get_item('TEMP').units)
        expect(temp.limits.values[:DEFAULT]).to eq([-80.0, -70.0, 60.0, 80.0, -20.0, 20.0])
        expect(temp.limits.values[:TVAC]).to eq([-60.0, -50.0, 40.0, 50.0])

        expect(result.get_item('MODE').states).to eq({ 'OFF' => 0, 'ON' => 1, 'ERROR' => 'ANY' })
        expect(result.get_item('SAMPLES').array_size).to eq(160)
        expect(result.get_item('SAMPLES').data_type).to eq(:INT)
        expect(result.get_item('LABEL').bit_size).to eq(64)
        expect(result.get_item('PAYLOAD').bit_size).to eq(0)
      end

      it "is fully lossless by canonical config diff (size and every serialized field)" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT KITCHEN BIG_ENDIAN "Kitchen sink packet"
            APPEND_ID_ITEM APID 8 UINT 1 "apid"
            APPEND_ITEM COUNTER 32 UINT "counter" LITTLE_ENDIAN
            APPEND_ITEM TEMP 16 UINT "temperature"
              POLY_READ_CONVERSION -100.0 0.00305
              UNITS Celsius C
              FORMAT_STRING "%.2f"
              LIMITS DEFAULT 1 ENABLED -80 -70 60 80 -20 20
              LIMITS TVAC 1 ENABLED -60 -50 40 50
              META SOURCE "sensor_a"
            APPEND_ITEM MODE 8 UINT "mode"
              STATE OFF 0
              STATE ON 1 GREEN
              STATE ERROR ANY
            APPEND_ARRAY_ITEM SAMPLES 16 INT 160 "ten signed samples"
            APPEND_ITEM LABEL 64 STRING "label"
            APPEND_ITEM PAYLOAD 0 BLOCK "variable payload"
        CFG
        original = pc.telemetry['TGT']['KITCHEN']
        reparsed, = round_trip(pc, 'TGT')
        result = reparsed.telemetry['TGT']['KITCHEN']
        # Size and every to_config-serialized field must match exactly.
        expect(original.defined_length_bits).to eq(result.defined_length_bits)
        expect_lossless(original, result, :TELEMETRY)
      end

      it "reports no differences from XtceConverter.compare for a faithful round trip" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT1 BIG_ENDIAN "Packet 1"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM A 16 UINT "a"
          TELEMETRY TGT PKT2 BIG_ENDIAN "Packet 2"
            APPEND_ID_ITEM ID 8 UINT 2 "id"
            APPEND_ITEM B 16 INT "b"
              LIMITS DEFAULT 1 ENABLED -80 -70 60 80
        CFG
        reparsed, = round_trip(pc, 'TGT')
        expect(XtceConverter.compare(pc, reparsed)).to be_empty
      end

      it "flags via XtceConverter.compare a packet present in one side only" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT1 BIG_ENDIAN "Packet 1"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM A 16 UINT "a"
          TELEMETRY TGT PKT2 BIG_ENDIAN "Packet 2"
            APPEND_ID_ITEM ID 8 UINT 2 "id"
            APPEND_ITEM B 16 UINT "b"
        CFG
        reparsed, = round_trip(pc, 'TGT')
        reparsed.telemetry['TGT'].delete('PKT2')
        diffs = XtceConverter.compare(pc, reparsed)
        expect(diffs).to have_key('TLM TGT PKT2')
        expect(diffs['TLM TGT PKT2'].first).to match(/not candidate/)
      end

      it "round-trips two targets exported in the same pass" do
        pc = config_from(<<~CFG)
          TELEMETRY TGT PKT BIG_ENDIAN "Target 1 packet"
            APPEND_ID_ITEM ID 8 UINT 1 "id"
            APPEND_ITEM A 16 UINT "a"
        CFG
        # A second target in the same PacketConfig
        tf = Tempfile.new(['rt_config2', '.txt'])
        tf.write(<<~CFG)
          TELEMETRY TGT2 PKT BIG_ENDIAN "Target 2 packet"
            APPEND_ID_ITEM ID 8 UINT 2 "id"
            APPEND_ITEM B 16 UINT "b"
        CFG
        tf.close
        @files << tf
        pc.process_file(tf.path, 'TGT2')

        reparsed1, = round_trip(pc, 'TGT')
        reparsed2, = round_trip(pc, 'TGT2')
        expect(reparsed1.telemetry['TGT']['PKT'].get_item('A')).not_to be_nil
        expect(reparsed2.telemetry['TGT2']['PKT'].get_item('B')).not_to be_nil
        expect(reparsed2.telemetry['TGT2']['PKT'].get_item('ID').id_value).to eq(2)
      end
    end
  end
end
